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
import re
import time
from typing import Literal

import duckdb
from pydantic import BaseModel, Field, ValidationError

from . import observabilidade as obs
from . import privacidade
from .config import DB_PATH

# gemini-2.0-flash foi desligado em 01/06/2026; o default acompanha o sucessor
# recomendado pelo Google. Sobrescreva com RADAR_LLM_MODEL se ele mudar de novo.
MODELO = os.environ.get("RADAR_LLM_MODEL", "gemini-3.5-flash")
# Modelo reserva, usado quando o principal responde 429/500/503 mesmo após as
# esperas. Vazio = sem reserva. Ex.: RADAR_LLM_FALLBACK=gemini-3.1-flash-lite
MODELO_RESERVA = os.environ.get("RADAR_LLM_FALLBACK", "").strip() or None
API_KEY = os.environ.get("GEMINI_API_KEY")
# Acima desta taxa de falha, algo está sistemicamente errado (chave inválida,
# modelo removido) e é melhor parar do que entregar um digest degradado.
LIMIAR_FALHA = 0.30

TABELA = "gold.gld_opportunities_enriched"

# Falhas transitórias do provedor: vale esperar e tentar de novo.
CODIGOS_TRANSITORIOS = {429, 500, 502, 503, 504}
# Falhas de configuração: a chave ou o modelo estão errados. Tentar de novo
# não adianta e o run deve parar na hora.
CODIGOS_CONFIGURACAO = {401, 403, 404}
ESPERAS_S = (5, 15, 45)   # backoff por modelo antes de desistir dele


class ErroConfiguracaoLLM(RuntimeError):
    """401/403/404: chave inválida, sem permissão ou modelo inexistente."""


class ErroTransitorioLLM(RuntimeError):
    """429/5xx persistente, mesmo após esperas e modelo reserva."""


class ConteudoRejeitado(ValueError):
    """O modelo respondeu, mas o texto viola as regras do prompt (duas vezes)."""


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
- A empresa JÁ ESTÁ ABERTA: o CNPJ já foi emitido. Não ofereça "abertura de
  empresa" nem "pacote de abertura". A oportunidade é contabilidade e BPO para
  quem acabou de começar a operar.
- NÃO afirme necessidade ou urgência como fato. Evite "necessita", "precisa",
  "urgente", "perfeitamente". Use "pode se beneficiar", "tende a".
- O regime tributário NÃO foi informado. Não cite Simples Nacional, MEI nem
  qualquer regime como se fosse a situação da empresa.
- Não cite canais de contato (telefone, e-mail, LinkedIn, WhatsApp, visita).
  O canal é escolha de quem usa o digest. Diga apenas "contato consultivo".
- Escreva em português do Brasil, com acentuação e cedilha corretas (ção, ã,
  é, ê, í, ó, ç). Nunca troque "ç" por "c" nem remova acentos. Não invente
  palavras.
- A explicação de relevância deve conectar o evento à oferta do ICP.

CONFIANÇA (da leitura comercial, não da detecção do evento):
- "media" é o padrão: você só tem CNAE, porte, local e capital.
- "alta" SÓ se a razão social ou o nome fantasia confirmar a atividade do CNAE.
- "baixa" se os dados forem genéricos ou se houver sinal de que a empresa já
  tem contador, pertence a um grupo ou está fora da região.
- Responda SOMENTE com JSON válido no esquema pedido."""


# Termos que o prompt proíbe. O modelo ainda os usa de vez em quando; em vez de
# confiar no prompt, o código confere e pede uma segunda resposta.
PROIBIDOS = re.compile(
    r"urgente|abertura de (empresa|cnpj|contabilidade)|"
    r"(pacote|serviços?|consultoria|assessoria|processo)s? (inicial )?de abertura|"
    r"abrir (a )?empresa|simples nacional|\bmei\b|linkedin|whatsapp|instagram|"
    r"presencial|\bvisitas?\b|telefone|e-?mail|estructur|empresa nueva",
    re.IGNORECASE)
# Afirmar necessidade da EMPRESA só é problema onde o texto fala dela. Em
# público_alvo, "empresas que necessitam de transporte" descreve os clientes.
PROIBIDOS_SOBRE_A_EMPRESA = re.compile(
    r"\bnecessit\w*|\bprecisa(m)?\b|perfeitamente", re.IGNORECASE)
# Palavras que, em português correto, SEMPRE levam acento ou cedilha. Se
# aparecerem sem, o modelo desacentuou o texto (o modelo leve faz isso quando
# imita o CNAE/município em caixa alta). Só minúsculas e Capitalizadas: a razão
# social em maiúsculas ("COMERCIO RDM") é nome próprio e não conta.
_SEM_ACENTO = (
    "servico servicos operacao operacoes organizacao constituicao obrigacao "
    "obrigacoes gestao inicio regiao apuracao conciliacao emissao estruturacao "
    "criacao medicoes especifico especificos especifica especificas fisica "
    "diario comercio alimentacao construcao edificios imoveis proprietarios "
    "industrias contabil contabeis coracoes recem acessorios vestuario "
    "logistica distribuicao prestacao negocio negocios").split()
ACENTO_PERDIDO = re.compile(
    r"\b(" + "|".join(sorted({w for p in _SEM_ACENTO for w in (p, p.capitalize())},
                             key=len, reverse=True)) + r")\b")
CAMPOS_TEXTO = ("atividade_provavel", "publico_alvo", "hipotese_de_dor",
                "explicacao_relevancia", "acao_sugerida")
CAMPOS_SOBRE_A_EMPRESA = ("explicacao_relevancia", "acao_sugerida")


def violacoes(ctx: dict) -> list[str]:
    """Trechos proibidos presentes nos campos de texto da leitura."""
    achados = []
    for campo in CAMPOS_TEXTO:
        texto = ctx.get(campo) or ""
        for m in PROIBIDOS.finditer(texto):
            achados.append(f"{campo}: '{m.group(0)}'")
        if campo in CAMPOS_SOBRE_A_EMPRESA:
            for m in PROIBIDOS_SOBRE_A_EMPRESA.finditer(texto):
                achados.append(f"{campo}: '{m.group(0)}'")
    todo = " ".join(ctx.get(c) or "" for c in CAMPOS_TEXTO)
    sem_acento = sorted({m.group(0).lower() for m in ACENTO_PERDIDO.finditer(todo)})
    if sem_acento:
        achados.append("texto sem acentuação: " + ", ".join(sem_acento[:5]))
    return achados


def calibrar_confianca(ctx: dict, row: dict) -> dict:
    """Teto de confiança vindo dos sinais do pipeline, que o modelo não vê com
    certeza: contador atual/grupo → baixa; DDD fora da região → no máximo média."""
    ordem = {"baixa": 0, "media": 1, "alta": 2}
    teto = "alta"
    if row.get("sinal_contador_atual") or row.get("sinal_grupo_empresarial"):
        teto = "baixa"
    elif row.get("sinal_fora_da_regiao"):
        teto = "media"
    if ordem[ctx["confianca"]] > ordem[teto]:
        ctx["confianca"] = teto
    return ctx


def _prompt(row: dict) -> str:
    row = privacidade.para_exibicao(row)   # nunca enviar nome de pessoa física ao LLM
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
- Indício de que já tem contador: {'sim' if row.get('sinal_contador_atual') else 'não'}
- Indício de filial, SPE ou grupo: {'sim' if row.get('sinal_grupo_empresarial') else 'não'}
- Telefone com DDD fora da região: {'sim' if row.get('sinal_fora_da_regiao') else 'não'}

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


def chamar_llm(client, row: dict, modelo: str | None = None,
               lembrete: str = "") -> dict:
    """Chama o modelo com saída estruturada. Levanta exceção em caso de falha —
    o tratamento (contagem, limiar) fica com o chamador."""
    from google.genai import types

    resp = client.models.generate_content(
        model=modelo or MODELO,
        contents=_prompt(row) + lembrete,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM,
            response_mime_type="application/json",
            response_schema=ContextoComercial,
            temperature=0.2,
        ),
    )
    dados = json.loads(resp.text)
    return ContextoComercial(**dados).model_dump()   # valida de novo


def _codigo_http(exc: Exception) -> int | None:
    """Código HTTP do erro do SDK (google.genai.errors.APIError.code)."""
    codigo = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    return codigo if isinstance(codigo, int) else None


def _chamar_modelo(client, row: dict, modelo: str, dormir) -> dict:
    """Um modelo, com backoff nas falhas transitórias (429/5xx).

    Config errada (401/403/404) levanta ErroConfiguracaoLLM na hora. Uma
    resposta com texto proibido é pedida uma segunda vez, com a lista do que
    corrigir; se repetir, ConteudoRejeitado.
    """
    esperas = list(ESPERAS_S)
    lembrete = ""
    reescrita_feita = False
    while True:
        try:
            ctx = chamar_llm(client, row, modelo, lembrete)
        except (ValidationError, json.JSONDecodeError):
            raise
        except Exception as e:  # noqa: BLE001
            codigo = _codigo_http(e)
            if codigo in CODIGOS_CONFIGURACAO:
                raise ErroConfiguracaoLLM(
                    f"HTTP {codigo} no modelo '{modelo}': verifique a chave de "
                    f"API (GEMINI_API_KEY) e se o modelo existe e está "
                    f"liberado para ela.") from e
            if codigo in CODIGOS_TRANSITORIOS:
                if not esperas:
                    raise ErroTransitorioLLM(
                        f"HTTP {codigo} persistente no modelo '{modelo}'.") from e
                espera = esperas.pop(0)
                print(f"  [RETRY] HTTP {codigo} em '{modelo}': nova tentativa "
                      f"em {espera}s")
                dormir(espera)
                continue
            raise
        achados = violacoes(ctx)
        if not achados:
            return calibrar_confianca(ctx, row)
        if reescrita_feita:
            raise ConteudoRejeitado(
                "texto ainda viola as regras do prompt: " + "; ".join(achados[:3]))
        reescrita_feita = True
        lembrete = ("\n\nSUA RESPOSTA ANTERIOR VIOLOU AS REGRAS (" +
                    "; ".join(achados) + "). Reescreva sem esses termos.")


def chamar_com_resiliencia(client, row: dict, dormir=None) -> tuple[dict, str]:
    """Modelo principal com backoff; se esgotar, o modelo reserva (se houver).
    Devolve (leitura, modelo realmente usado). `dormir` é injetável nos testes;
    resolvido na chamada para que time.sleep possa ser substituído."""
    dormir = dormir or time.sleep
    modelos = [MODELO] + ([MODELO_RESERVA] if MODELO_RESERVA and
                          MODELO_RESERVA != MODELO else [])
    ultimo: Exception | None = None
    for modelo in modelos:
        try:
            return _chamar_modelo(client, row, modelo, dormir), modelo
        except ErroTransitorioLLM as e:
            ultimo = e
            if modelo != modelos[-1]:
                print(f"  [RESERVA] '{modelo}' indisponível; tentando "
                      f"'{modelos[-1]}'")
    assert ultimo is not None
    raise ultimo


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


def _linha(row: dict, ctx: dict) -> tuple:
    return (
        row["event_id"], ctx["atividade_provavel"], ctx["publico_alvo"],
        ctx["hipotese_de_dor"], ctx["explicacao_relevancia"],
        ctx["acao_sugerida"], ctx["confianca"], ctx["origem"], ctx["modelo"],
    )


def _gravar(con, linhas: list[tuple]) -> None:
    if linhas:
        con.executemany(
            f"INSERT INTO {TABELA} VALUES (?,?,?,?,?,?,?,?,?, current_timestamp)",
            linhas)


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

    linhas, falhas, n_llm, n_heur, tentativas = [], 0, 0, 0, 0
    transitorias = 0
    print(f"[PROCESSANDO] {len(pendentes)} oportunidade(s)...")

    for row in pendentes:
        # Empresário individual não vai ao LLM: o digest o mostra numa tabela
        # compacta, sem leitura comercial, e o nome é de pessoa física.
        if client and not privacidade.deve_proteger(row):
            tentativas += 1
            try:
                ctx, modelo_usado = chamar_com_resiliencia(client, row)
                ctx["origem"], ctx["modelo"] = "llm", modelo_usado
                n_llm += 1
                time.sleep(4)          # respeita o free tier (~15 req/min)
            except ErroConfiguracaoLLM:
                # Chave/modelo errados: continuar só geraria heurística em massa.
                _gravar(con, linhas)
                con.close()
                raise
            except (ValidationError, Exception) as e:  # noqa: BLE001
                falhas += 1
                if isinstance(e, ErroTransitorioLLM):
                    transitorias += 1
                print(f"  [FALHA {falhas}] evento {row['event_id'][:8]}: "
                      f"{type(e).__name__}: {str(e)[:120]}")
                ctx = heuristica(row)
                ctx["origem"], ctx["modelo"] = "heuristica", None
                n_heur += 1
                # Falha sistêmica: parar em vez de entregar digest degradado.
                if tentativas >= 5 and falhas / tentativas > LIMIAR_FALHA:
                    # Guarda o que já foi lido: as chamadas anteriores custaram
                    # cota e não devem ser refeitas na próxima execução.
                    _gravar(con, linhas + [_linha(row, ctx)])
                    con.close()
                    if transitorias >= falhas / 2:
                        dica = ("o provedor está sobrecarregado ou o limite de "
                                "uso acabou. Tente mais tarde ou defina "
                                "RADAR_LLM_FALLBACK com outro modelo.")
                    else:
                        dica = (f"as respostas do modelo '{MODELO}' estão "
                                f"inválidas ou fora das regras do prompt.")
                    raise RuntimeError(
                        f"Taxa de falha do LLM em {falhas}/{tentativas} "
                        f"(>{LIMIAR_FALHA:.0%}): {dica}") from e
        else:
            ctx = heuristica(row)
            ctx["origem"], ctx["modelo"] = "heuristica", None
            n_heur += 1

        linhas.append(_linha(row, ctx))

    _gravar(con, linhas)
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
