"""Enriquecimento semântico — camada de INTERPRETAÇÃO.

Princípios 6 e 7 do Plano Diretor: a IA nunca é fonte da verdade e nunca
inventa dados. O pipeline já decidiu o que aconteceu (evento) e quanto vale
(score); o modelo apenas interpreta, explica e sugere ação.

Correções em relação à versão anterior:
  * O fallback fabricava texto e o marcava como "confiança Alta", indistinguível
    de saída real no digest. Agora a origem é sempre explícita
    (`origem` = 'llm' | 'heuristica' | 'falha') e o digest é obrigado a exibir.
  * `except Exception` engolia QUALQUER falha (chave inválida, modelo
    aposentado, rate limit) e a substituía por texto inventado. Agora as falhas
    são contadas, reportadas e, acima de um limiar, interrompem a execução.
  * Não havia cache: cada execução re-enriquecia tudo e re-gastava tokens.
    Agora só processa event_id inédito.
  * O prompt não tinha grounding, não recebia o ICP nem o score.
  * SDK/modelo defasados (`google.generativeai`, `gemini-1.5-flash`).
    Agora usa `google-genai` com modelo configurável por variável de ambiente.

Uso:
    export GEMINI_API_KEY=...
    python -m src.enriquecimento
    python -m src.enriquecimento --sem-ia     # roda só com heurística
"""
from __future__ import annotations

import argparse
import json
import os
import time
from typing import Literal

import duckdb
from pydantic import BaseModel, Field, ValidationError

from . import observabilidade as obs
from .config import DB_PATH

# gemini-2.0-flash foi desligado em 01/06/2026; o default acompanha o sucessor
# recomendado pelo Google. Sobrescreva com RADAR_LLM_MODEL se ele mudar de novo.
MODELO = os.environ.get("RADAR_LLM_MODEL", "gemini-3.5-flash")
API_KEY = os.environ.get("GEMINI_API_KEY")
# Acima desta taxa de falha, algo está sistemicamente errado (chave inválida,
# modelo removido) e é melhor parar do que entregar um digest degradado.
LIMIAR_FALHA = 0.30

TABELA = "gold.gld_opportunities_enriched"


# --------------------------------------------------------------- contrato
class ContextoComercial(BaseModel):
    """Contrato rígido de saída. Campos são INTERPRETAÇÃO, não fato."""

    atividade_provavel: str = Field(
        description="O que a empresa provavelmente faz, inferido do CNAE.")
    publico_alvo: str = Field(
        description="Prováveis clientes desta empresa.")
    hipotese_de_dor: str = Field(
        description="HIPÓTESE de dor operacional típica do setor/estágio. "
                    "Não é um fato apurado sobre esta empresa.")
    explicacao_relevancia: str = Field(
        description="Por que este evento importa para o ICP informado.")
    acao_sugerida: str = Field(
        description="Próximo passo comercial concreto.")
    confianca: Literal["alta", "media", "baixa"] = Field(
        description="Confiança da interpretação, dada a escassez dos dados.")


SYSTEM = """Você é um analista comercial B2B. Recebe fatos JÁ VALIDADOS por um
pipeline de dados públicos da Receita Federal. Você NÃO decide o que aconteceu
nem calcula o score — isso já foi feito.

REGRAS INEGOCIÁVEIS:
- Infira APENAS a partir dos campos fornecidos.
- NUNCA invente faturamento, número de funcionários, nomes de sócios,
  contatos, patrimônio ou qualquer dado ausente.
- O campo hipotese_de_dor é uma HIPÓTESE setorial, não um fato sobre a empresa.
  Redija-o como hipótese ("empresas deste setor costumam...").
- Se os dados forem genéricos demais para uma leitura útil, use
  confianca = "baixa".
- A explicação de relevância deve conectar o evento à oferta do ICP.
- Responda SOMENTE com JSON válido no esquema pedido."""


def _prompt(row: dict) -> str:
    return f"""EVENTO DETECTADO: {row['event_type']} em {row['event_date']}
(confiança da detecção: {row['detection_confidence']})

EMPRESA (dados públicos da Receita):
- Razão social: {row.get('razao_social') or 'não informada'}
- Nome fantasia: {row.get('nome_fantasia') or 'não informado'}
- CNAE principal: {row.get('cnae_descricao') or 'não informado'}
- Município/UF: {row.get('municipio_nome')}/{row.get('uf')}
- Porte: {row.get('porte_descricao') or 'não informado'}
- Capital social: {row.get('capital_social') if row.get('capital_social') is not None else 'não informado'}

CLIENTE (ICP) QUE VAI RECEBER ESTA OPORTUNIDADE:
- Perfil: {row.get('icp_nome')}
- Oferta: {row.get('icp_oferta')}

PRIORIZAÇÃO JÁ CALCULADA PELO PIPELINE:
- Score: {row.get('opportunity_score')}/100
- Fatores: {row.get('score_fatores')}

Interprete e responda no esquema JSON."""


def heuristica(row: dict) -> dict:
    """Fallback SEM IA — determinístico e honesto.

    Não tenta imitar análise: monta um texto a partir apenas dos dados que
    realmente existem. É marcado como origem='heuristica' e nunca se apresenta
    como interpretação de IA.
    """
    cnae = row.get("cnae_descricao") or "atividade não informada"
    municipio = row.get("municipio_nome") or "município não informado"
    return {
        "atividade_provavel": f"Atividade cadastrada: {cnae}.",
        "publico_alvo": "Não inferido (enriquecimento por IA indisponível).",
        "hipotese_de_dor": "Não inferido (enriquecimento por IA indisponível).",
        "explicacao_relevancia": (
            f"Empresa aberta há {row.get('dias_desde_evento')} dias em "
            f"{municipio}, dentro do perfil configurado. "
            f"Score {row.get('opportunity_score')}/100."),
        "acao_sugerida": "Revisar manualmente antes de abordar.",
        "confianca": "baixa",
    }


# ------------------------------------------------------------------- LLM
def _cliente():
    """Instancia o cliente do SDK atual (google-genai)."""
    from google import genai  # import tardio: só exigido quando há IA
    return genai.Client(api_key=API_KEY)


def chamar_llm(client, row: dict) -> dict:
    """Chama o modelo com saída estruturada. Levanta exceção em caso de falha —
    o tratamento (contagem, limiar) fica com o chamador."""
    from google.genai import types

    resp = client.models.generate_content(
        model=MODELO,
        contents=_prompt(row),
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM,
            response_mime_type="application/json",
            response_schema=ContextoComercial,
            temperature=0.2,
        ),
    )
    dados = json.loads(resp.text)
    return ContextoComercial(**dados).model_dump()   # valida de novo


# ------------------------------------------------------------------ main
DDL = f"""
CREATE SCHEMA IF NOT EXISTS gold;
CREATE TABLE IF NOT EXISTS {TABELA} (
    event_id              VARCHAR PRIMARY KEY,
    atividade_provavel    VARCHAR,
    publico_alvo          VARCHAR,
    hipotese_de_dor       VARCHAR,
    explicacao_relevancia VARCHAR,
    acao_sugerida         VARCHAR,
    confianca             VARCHAR,
    origem                VARCHAR,   -- llm | heuristica
    modelo                VARCHAR,
    enriquecido_em        TIMESTAMP
);
"""


def enriquecer(usar_ia: bool = True, limite: int | None = None) -> dict:
    con = duckdb.connect(str(DB_PATH))
    con.execute(DDL)

    # CACHE / IDEMPOTÊNCIA: só o que ainda não foi enriquecido.
    sql = f"""
        SELECT o.*
        FROM main_gold.gld_opportunities o
        WHERE NOT EXISTS (
            SELECT 1 FROM {TABELA} e WHERE e.event_id = o.event_id
        )
        ORDER BY o.opportunity_score DESC
    """
    if limite:
        sql += f" LIMIT {limite}"

    cursor = con.execute(sql)
    colunas = [d[0] for d in cursor.description]
    pendentes = [dict(zip(colunas, r, strict=True)) for r in cursor.fetchall()]

    total_existente = con.execute(f"SELECT count(*) FROM {TABELA}").fetchone()[0]
    print(f"[CACHE] {total_existente} oportunidade(s) já enriquecidas — puladas.")

    if not pendentes:
        print("[OK] Nada novo para enriquecer.")
        con.close()
        return {"processadas": 0, "llm": 0, "heuristica": 0, "falhas": 0}

    client = None
    if usar_ia and API_KEY:
        try:
            client = _cliente()
            print(f"[IA] Cliente pronto. Modelo: {MODELO}")
        except Exception as e:  # noqa: BLE001
            print(f"[ERRO] Não foi possível iniciar o cliente de IA: {e}")
            print("[ERRO] Seguindo com heurística — o digest será marcado.")
    elif usar_ia:
        print("[AVISO] GEMINI_API_KEY ausente. Sem enriquecimento por IA.")
        print("[AVISO] O digest sairá marcado como NÃO ENRIQUECIDO.")

    linhas, falhas, n_llm, n_heur = [], 0, 0, 0
    print(f"[PROCESSANDO] {len(pendentes)} oportunidade(s)...")

    for i, row in enumerate(pendentes, 1):
        if client:
            try:
                ctx = chamar_llm(client, row)
                ctx["origem"], ctx["modelo"] = "llm", MODELO
                n_llm += 1
                time.sleep(4)          # respeita o free tier (~15 req/min)
            except (ValidationError, Exception) as e:  # noqa: BLE001
                falhas += 1
                print(f"  [FALHA {falhas}] evento {row['event_id'][:8]}: "
                      f"{type(e).__name__}: {str(e)[:120]}")
                ctx = heuristica(row)
                ctx["origem"], ctx["modelo"] = "heuristica", None
                n_heur += 1
                # Falha sistêmica: parar em vez de entregar digest degradado.
                if i >= 5 and falhas / i > LIMIAR_FALHA:
                    con.close()
                    raise RuntimeError(
                        f"Taxa de falha do LLM em {falhas}/{i} "
                        f"(>{LIMIAR_FALHA:.0%}). Interrompendo: verifique a "
                        f"chave de API e se o modelo '{MODELO}' está ativo.") from e
        else:
            ctx = heuristica(row)
            ctx["origem"], ctx["modelo"] = "heuristica", None
            n_heur += 1

        linhas.append((
            row["event_id"], ctx["atividade_provavel"], ctx["publico_alvo"],
            ctx["hipotese_de_dor"], ctx["explicacao_relevancia"],
            ctx["acao_sugerida"], ctx["confianca"], ctx["origem"], ctx["modelo"],
        ))

    con.executemany(
        f"INSERT INTO {TABELA} VALUES (?,?,?,?,?,?,?,?,?, current_timestamp)",
        linhas)
    con.close()

    print(f"[OK] {len(linhas)} enriquecida(s): {n_llm} por IA, "
          f"{n_heur} por heurística, {falhas} falha(s).")
    return {"processadas": len(linhas), "llm": n_llm,
            "heuristica": n_heur, "falhas": falhas}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sem-ia", action="store_true",
                    help="não chama o LLM; usa apenas a heurística")
    ap.add_argument("--limite", type=int, default=None)
    args = ap.parse_args()

    print("--- ENRIQUECIMENTO SEMÂNTICO ---")
    with obs.etapa("enriquecimento") as ctx:
        r = enriquecer(usar_ia=not args.sem_ia, limite=args.limite)
        ctx["registros"] = r["processadas"]
        ctx["chamadas_llm"] = r["llm"]
        ctx["detalhe"] = json.dumps(r)


if __name__ == "__main__":
    main()
