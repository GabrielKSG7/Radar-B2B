"""Geração do Digest — o entregável do MVP.

Correções em relação à versão anterior:
  * Identificava a empresa só pelo CNPJ ("Oportunidade: CNPJ 11111111000199").
    Um contador não consegue agir sobre isso. Agora traz razão social, nome
    fantasia, porte, capital e contato.
  * Renderizava texto fabricado sob o título "Inteligência Comercial (IA)"
    sem distinguir da saída real. Agora a procedência é sempre estampada, e
    um digest sem IA leva aviso no topo.
  * Não havia priorização: listava tudo em sequência. Agora separa em
    prioridade alta/média/baixa, conforme o Plano Diretor (§12).
  * Não havia captura de feedback (§13). Agora cada oportunidade sai com um
    campo de marcação, e o digest é registrado para acompanhamento.

Uso:
    python -m src.digest
    python -m src.digest --top 15
"""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime

import duckdb

from . import observabilidade as obs
from . import privacidade
from .config import DB_PATH, DIGEST_DIR

FAIXAS = [("Alta", 70), ("Média", 50), ("Baixa", 0)]

DDL_FEEDBACK = """
CREATE SCHEMA IF NOT EXISTS gold;
CREATE TABLE IF NOT EXISTS gold.opportunity_feedback (
    event_id        VARCHAR,
    icp_nome        VARCHAR,
    digest_data     DATE,
    relevante       BOOLEAN,      -- preenchido pelo usuário
    motivo          VARCHAR,
    contatada       BOOLEAN,
    desfecho        VARCHAR,      -- resposta | reuniao | proposta | contrato
    registrado_em   TIMESTAMP
);
"""


def _faixa(score: int) -> str:
    for nome, minimo in FAIXAS:
        if score >= minimo:
            return nome
    return "Baixa"


def _dias(n) -> str:
    if n is None:
        return "data desconhecida"
    if n == 0:
        return "hoje"
    return "há 1 dia" if n == 1 else f"há {n} dias"


def _fmt_capital(valor) -> str:
    if valor is None:
        return "não informado"
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _bloco(row: dict) -> str:
    nome = row.get("razao_social") or row.get("nome_fantasia") or "(razão social não disponível)"
    fantasia = row.get("nome_fantasia")
    titulo = nome if not fantasia or fantasia == nome else f"{nome} ({fantasia})"

    contato = []
    if row.get("telefone"):
        contato.append(f"tel. {row['telefone']}")
    if row.get("email"):
        contato.append(row["email"])
    contato_txt = " · ".join(contato) if contato else "sem contato no cadastro"

    fatores = row.get("score_fatores")
    if isinstance(fatores, str):
        try:
            fatores = json.loads(fatores)
        except json.JSONDecodeError:
            fatores = {}
    fatores_txt = ", ".join(f"{k} +{v}" for k, v in (fatores or {}).items() if v)

    # Procedência do enriquecimento — sempre explícita.
    if row.get("origem") == "llm":
        selo = f"interpretado por IA ({row.get('modelo')}), confiança {row.get('confianca')}"
    else:
        selo = "SEM INTERPRETAÇÃO DE IA — texto derivado apenas do cadastro"

    return f"""
### {titulo}
**CNPJ:** `{row['cnpj']}` · **Score:** {row['opportunity_score']}/100 ({_faixa(row['opportunity_score'])})

| | |
|---|---|
| **Evento** | {row['event_type']} — aberta em {row['event_date']} ({_dias(row['dias_desde_evento'])}) |
| **Onde** | {row.get('municipio_nome')}/{row.get('uf')} |
| **Setor** | {row.get('cnae_descricao')} (`{row.get('cnae_principal')}`) |
| **Porte** | {row.get('porte_descricao')} · capital {_fmt_capital(row.get('capital_social'))} |
| **Contato** | {contato_txt} |

**Por que apareceu:** {row.get('score_explanation')}
*Fatores: {fatores_txt}*

**Leitura comercial** — <sub>{selo}</sub>
- **Atividade provável:** {row.get('atividade_provavel')}
- **Público-alvo:** {row.get('publico_alvo')}
- **Hipótese de dor (não é fato apurado):** {row.get('hipotese_de_dor')}
- **Relevância:** {row.get('explicacao_relevancia')}

**Ação sugerida:** {row.get('acao_sugerida')}

`[ ] relevante   [ ] irrelevante   [ ] contatada` — motivo: ______________

---
"""


def _secao_individuais(rows: list[dict]) -> list[str]:
    """Empresários individuais: tabela compacta, nome mascarado, sem contato."""
    if not rows:
        return []
    md = [
        f"\n## Empresários individuais ({len(rows)})\n",
        "> Nome e contato omitidos neste piloto: em empresário individual a razão "
        "social costuma ser o nome da pessoa. O CNPJ segue sendo a identificação "
        "pública do cadastro.\n",
        "| # | Score | Setor | Onde | Porte | Capital | Aberta em | Nome fantasia | CNPJ | Marcação |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for n, r in enumerate(rows, 1):
        x = privacidade.para_exibicao(r)
        md.append(
            f"| {n} | {r['opportunity_score']} | {r.get('cnae_descricao')} "
            f"(`{r.get('cnae_principal')}`) | {r.get('municipio_nome')}/{r.get('uf')} "
            f"| {r.get('porte_descricao')} | {_fmt_capital(r.get('capital_social'))} "
            f"| {r['event_date']} | {x.get('nome_fantasia') or '—'} | `{r['cnpj']}` "
            f"| [ ] relevante · [ ] contatada |")
    md.append("\n---\n")
    return md


CAMPOS_CSV = [
    "secao", "score", "faixa", "razao_social", "nome_fantasia", "cnpj",
    "municipio", "uf", "cnae", "cnae_descricao", "porte", "capital_social",
    "aberta_em", "telefone", "email", "por_que_apareceu",
    "relevante", "contatada", "motivo",
]


def _exportar_csv(linhas: list[dict], caminho) -> None:
    """Planilha do piloto. ';' e UTF-8 com BOM: abre direto no Excel em pt-BR."""
    with open(caminho, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(CAMPOS_CSV)
        for r in linhas:
            x = privacidade.para_exibicao(r)
            w.writerow([
                "Empresário individual" if privacidade.deve_proteger(r) else "Empresa",
                r["opportunity_score"], _faixa(r["opportunity_score"]),
                x.get("razao_social"), x.get("nome_fantasia"), r["cnpj"],
                r.get("municipio_nome"), r.get("uf"), r.get("cnae_principal"),
                r.get("cnae_descricao"), r.get("porte_descricao"),
                r.get("capital_social"), r["event_date"],
                x.get("telefone"), x.get("email"), r.get("score_explanation"),
                "", "", ""])


def gerar(top: int | None = None) -> tuple[str, int]:
    con = duckdb.connect(str(DB_PATH))
    con.execute(DDL_FEEDBACK)

    sql = """
        SELECT o.*, e.atividade_provavel, e.publico_alvo, e.hipotese_de_dor,
               e.explicacao_relevancia, e.acao_sugerida, e.confianca,
               e.origem, e.modelo
        FROM main_gold.gld_opportunities o
        LEFT JOIN gold.gld_opportunities_enriched e USING (event_id)
        ORDER BY o.opportunity_score DESC
    """
    if top:
        sql += f" LIMIT {top}"
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    linhas = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    if not linhas:
        print("[AVISO] Nenhuma oportunidade para o digest.")
        con.close()
        return "", 0

    competencia = linhas[0].get("source_competence")
    # A Receita publica o snapshot no meio do mês: a idade dos eventos é medida
    # contra o último dia com volume real, e o leitor precisa saber qual é.
    ancora = linhas[0].get("data_ancora_recencia")
    icp = linhas[0].get("icp_nome")
    hoje = datetime.now().strftime("%Y-%m-%d")

    # Privacidade do piloto: empresário individual vai para seção própria, com
    # nome mascarado e sem contato (src/privacidade.py).
    empresas = [r for r in linhas if not privacidade.deve_proteger(r)]
    individuais = [r for r in linhas if privacidade.deve_proteger(r)]
    sem_ia = sum(1 for r in empresas if r.get("origem") != "llm")

    por_faixa: dict[str, list[dict]] = {"Alta": [], "Média": [], "Baixa": []}
    for r in empresas:
        por_faixa[_faixa(r["opportunity_score"])].append(r)

    md = ["# Radar B2B — Digest de Oportunidades\n",
          f"**Data:** {hoje}  ",
          f"**Perfil (ICP):** {icp}  ",
          f"**Competência analisada:** {competencia}  ",
          *([f"**Dados da Receita até:** {ancora}  "] if ancora else []),
          f"**Oportunidades:** {len(linhas)} — empresas {len(empresas)} "
          f"(alta {len(por_faixa['Alta'])} · média {len(por_faixa['Média'])} "
          f"· baixa {len(por_faixa['Baixa'])}) · "
          f"empresários individuais {len(individuais)}\n"]

    if sem_ia:
        md.append(
            f"> **Atenção:** {sem_ia} de {len(empresas)} empresas estão "
            f"**sem interpretação de IA**. Os campos de leitura comercial "
            f"foram derivados apenas do cadastro público, não de análise. "
            f"Verifique a configuração do enriquecimento.\n")

    md.append(
        "> As oportunidades vêm de dados públicos da Receita Federal. "
        "Os campos de leitura comercial são interpretação/hipótese, não fato "
        "apurado sobre a empresa.\n\n---\n")

    for faixa, _ in FAIXAS:
        grupo = por_faixa[faixa]
        if not grupo:
            continue
        md.append(f"\n## Empresas — prioridade {faixa} ({len(grupo)})\n")
        md.extend(_bloco(r) for r in grupo)

    md.extend(_secao_individuais(individuais))

    md.append(
        f"\n<sub>Gerado pelo Radar B2B em {hoje}. "
        f"Devolva este arquivo com as marcações de feedback — elas calibram "
        f"a priorização das próximas rodadas.</sub>\n")

    conteudo = "\n".join(md)

    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    saida = DIGEST_DIR / f"radar_digest_{icp}_{hoje}.md"
    saida.write_text(conteudo, encoding="utf-8")
    _exportar_csv(linhas, DIGEST_DIR / f"radar_piloto_{icp}_{hoje}.csv")

    # Registra as oportunidades enviadas, para o ciclo de feedback (§13).
    con.executemany(
        """INSERT INTO gold.opportunity_feedback
           (event_id, icp_nome, digest_data, registrado_em)
           VALUES (?,?,?, current_timestamp)""",
        [(r["event_id"], r.get("icp_nome"), hoje) for r in linhas])
    con.close()

    print(f"[OK] Digest gerado: {saida}")
    print(f"[OK] Planilha do piloto: radar_piloto_{icp}_{hoje}.csv "
          f"({len(empresas)} empresas, {len(individuais)} empresários individuais)")
    if sem_ia:
        print(f"[AVISO] {sem_ia} oportunidade(s) sem interpretação de IA "
              f"— o digest está marcado.")
    return str(saida), len(linhas)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=None,
                    help="limita as N melhores oportunidades")
    args = ap.parse_args()

    print("--- GERAÇÃO DO DIGEST ---")
    with obs.etapa("digest") as ctx:
        caminho, n = gerar(args.top)
        ctx["registros"] = n
        ctx["detalhe"] = caminho


if __name__ == "__main__":
    main()
