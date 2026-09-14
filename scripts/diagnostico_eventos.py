"""Sanidade dos eventos NEW_COMPANY detectados.

Responde a uma pergunta só, mas decisiva: os eventos detectados são aberturas
de empresa de verdade, ou artefato de ingestão?

O risco concreto: o set difference só mede abertura de empresa se as duas
competências cobrirem a MESMA população. Se cobrirem recortes diferentes, as
empresas presentes só na carga atual aparecem como novas — elas faltavam no mês
anterior porque não foram ingeridas, não porque não existiam.

Isso acontece de duas formas:
  * fatias diferentes entre as competências (uma com 0, outra com 0,1);
  * fatias iguais, mas a fatia não sendo estável entre publicações — o mesmo
    CNPJ caindo em arquivos diferentes a cada mês. Neste caso não há atalho:
    o CDC exige TODAS as fatias nas duas competências.

O teste `assert_evento_nao_existe_na_competencia_anterior` NÃO pega nenhum dos
dois: sob o critério dele essas empresas são legitimamente ausentes do mês
anterior.

Duas evidências separam artefato de evento real:
  * a população: empresa não some do cadastro em massa de um mês para o outro;
  * a data de início de atividade: abertura real cai na janela entre as
    competências, artefato carrega a data antiga que a empresa sempre teve.

Uso:
    python scripts/diagnostico_eventos.py
    python scripts/diagnostico_eventos.py --competencia 2024-08 --anterior 2024-07
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import BRONZE_DIR, DB_PATH  # noqa: E402

# Acima desta fração de eventos com data anterior à janela, o resultado não é
# "algumas inclusões retroativas" e sim ingestão assimétrica.
LIMIAR_SUSPEITA = 0.30

# Fração de CNPJs que somem de um mês para o outro. Baixas existem, mas são
# raras: acima disto as duas cargas não descrevem a mesma população.
LIMIAR_SUMICO = 0.02


def _tabela(con, nome: str) -> str | None:
    """Acha a tabela pelo nome, sem depender do schema que o dbt usou."""
    achado = con.execute(
        "SELECT schema_name FROM duckdb_tables() WHERE table_name = ? LIMIT 1", [nome]
    ).fetchone()
    return f'"{achado[0]}"."{nome}"' if achado else None


def _competencias_no_bronze(tabela: str) -> dict[str, int]:
    """Quantos arquivos de partição existem por competência, direto do disco."""
    base = BRONZE_DIR / tabela
    if not base.exists():
        return {}
    out = {}
    for pasta in sorted(base.glob("competencia=*")):
        out[pasta.name.split("=", 1)[1]] = len(list(pasta.glob("*.parquet")))
    return out


def diagnosticar(competencia: str | None, anterior: str | None) -> int:
    if not DB_PATH.exists():
        print(f"[ERRO] Banco não encontrado em {DB_PATH}. Rode o pipeline antes.")
        return 1

    con = duckdb.connect(str(DB_PATH), read_only=True)
    evt = _tabela(con, "evt_new_company")
    if not evt:
        print("[ERRO] Tabela evt_new_company não existe. O dbt rodou?")
        return 1

    total = con.execute(f"SELECT count(*) FROM {evt}").fetchone()[0]

    # Com a tabela vazia não há competência a extrair dela — caímos para o que
    # existe no Bronze, que é justamente o que precisa ser comparado com o que
    # o dbt processou.
    no_bronze = sorted(_competencias_no_bronze("estabelecimentos"))
    if competencia is None:
        competencia = con.execute(
            f"SELECT max(source_competence) FROM {evt}").fetchone()[0]
        if competencia is None and no_bronze:
            competencia = no_bronze[-1]
    if anterior is None:
        anterior = con.execute(
            f"SELECT max(compared_competence) FROM {evt}").fetchone()[0]
        if anterior is None and len(no_bronze) > 1:
            anterior = no_bronze[-2]

    print("=" * 66)
    print(f"DIAGNÓSTICO DE EVENTOS — {anterior} -> {competencia}")
    print("=" * 66)

    # ---------------------------------------------------------- volumetria
    print("\n[1] Volume no Bronze (estabelecimentos por competência)")
    for comp, n in sorted(_competencias_no_bronze("estabelecimentos").items()):
        print(f"      {comp}: {n} arquivo(s) de partição")

    tab_estab = _tabela(con, "slv_estabelecimentos")
    if tab_estab:
        for comp in (anterior, competencia):
            n = con.execute(
                f"SELECT count(*) FROM {tab_estab} WHERE competencia = ?", [comp]
            ).fetchone()[0]
            print(f"      {comp}: {n:>12,} estabelecimentos no Silver")

    # -------------------------------------------------------------- eventos
    print(f"\n[2] Eventos NEW_COMPANY detectados: {total:,}")
    if total == 0:
        # Zero eventos NÃO é "nada a analisar": com duas competências no
        # Bronze, é praticamente impossível que nenhuma empresa tenha aberto.
        # Tabela vazia não gera erro no dbt, então esta é a única forma de o
        # problema aparecer.
        print("\n" + "=" * 66)
        print("VEREDITO: nenhum evento detectado — isto é uma FALHA, não um resultado.")
        print("\n  Duas competências no Bronze e zero aberturas é implausível.")
        print("  O dbt constrói tabela vazia sem reclamar, então o pipeline")
        print("  termina 'com sucesso' e o digest sai vazio.")
        print("\n  Causas prováveis, em ordem:")
        print("   1. O dbt processou uma competência que não está no banco.")
        print("      `dbt run` sem --vars usa o default de dbt_project.yml, que")
        print("      pode apontar para meses que você nunca ingeriu. Confira:")
        if no_bronze:
            print(f"        Bronze tem: {', '.join(no_bronze)}")
        print("        dbt_project.yml -> vars.competencia_atual / _anterior")
        print("   2. Filtro de recência no Gold medindo contra a data de hoje")
        print("      em vez do fim da competência (some tudo em dado histórico).")
        print("   3. situacao_cadastral <> '02' em toda a base — improvável.")
        print("=" * 66)
        con.close()
        return 1

    # ------------------------------------------- estabilidade da população
    # Antes de julgar as datas: as duas competências descrevem a MESMA
    # população? Num snapshot mensal, empresa quase não desaparece. Se muitos
    # CNPJs existem só no mês anterior, os dois Bronze não são comparáveis — e
    # aí o set difference mede recorte de ingestão, não abertura de empresa.
    so_anterior = so_atual = None
    frac_sumico = 0.0
    if tab_estab:
        so_anterior, so_atual = con.execute(f"""
            WITH ant AS (SELECT cnpj FROM {tab_estab} WHERE competencia = ?),
                 atu AS (SELECT cnpj FROM {tab_estab} WHERE competencia = ?)
            SELECT (SELECT count(*) FROM (SELECT cnpj FROM ant EXCEPT SELECT cnpj FROM atu)),
                   (SELECT count(*) FROM (SELECT cnpj FROM atu EXCEPT SELECT cnpj FROM ant))
        """, [anterior, competencia]).fetchone()

        n_ant = con.execute(
            f"SELECT count(*) FROM {tab_estab} WHERE competencia = ?", [anterior]
        ).fetchone()[0]
        frac_sumico = so_anterior / n_ant if n_ant else 0.0

        print("\n[2.1] As duas competências descrevem a mesma população?")
        print(f"      só em {anterior} (sumiram) ... {so_anterior:>12,}  ({frac_sumico:.1%})")
        print(f"      só em {competencia} (novos) ..... {so_atual:>12,}")
        if frac_sumico > LIMIAR_SUMICO:
            print("      ^ ALERTA: empresa não some do cadastro em massa de um mês")
            print("        para o outro. As duas cargas cobrem populações")
            print("        DIFERENTES — provavelmente as fatias não são estáveis")
            print("        entre competências (o mesmo CNPJ cai em fatias")
            print("        diferentes a cada publicação da Receita).")

    # A janela plausível começa no primeiro dia da competência anterior: uma
    # empresa detectada como nova em `competencia` deve ter aberto por volta
    # dessa virada, não anos antes.
    inicio_janela = f"{anterior}-01"
    dentro, fora, sem_data = con.execute(f"""
        SELECT
            count(*) FILTER (WHERE event_date >= DATE '{inicio_janela}'),
            count(*) FILTER (WHERE event_date <  DATE '{inicio_janela}'),
            count(*) FILTER (WHERE event_date IS NULL)
        FROM {evt}
    """).fetchone()

    print(f"\n[3] Data de início de atividade (janela a partir de {inicio_janela})")
    print(f"      dentro da janela .... {dentro:>12,}  ({dentro/total:6.1%})  <- aberturas plausíveis")
    print(f"      anterior à janela ... {fora:>12,}  ({fora/total:6.1%})  <- suspeitas")
    print(f"      sem data ............ {sem_data:>12,}  ({sem_data/total:6.1%})")

    print("\n[4] Distribuição por ano de abertura (10 maiores)")
    for ano, n in con.execute(f"""
        SELECT year(event_date) AS ano, count(*) AS n FROM {evt}
        WHERE event_date IS NOT NULL
        GROUP BY 1 ORDER BY n DESC LIMIT 10
    """).fetchall():
        barra = "#" * max(1, round(40 * n / total))
        print(f"      {ano}  {n:>10,}  {barra}")

    # ------------------------------------------------------------- veredito
    print("\n" + "=" * 66)
    proporcao_fora = fora / total
    if proporcao_fora > LIMIAR_SUSPEITA:
        print("VEREDITO: eventos NÃO confiáveis.")
        print(f"  {proporcao_fora:.1%} dos eventos são de empresas abertas ANTES da")
        print(f"  competência anterior ({anterior}). Empresa aberta em 2015 não pode")
        print("  ser 'nova' em 2024: ela sumiu do mês anterior porque não foi")
        print("  ingerida, não porque não existia.")
        print("\n  A distribuição por ano acima é o retrato etário da base ativa,")
        print("  não de aberturas recentes — assinatura de amostra, não de evento.")
        if frac_sumico > LIMIAR_SUMICO:
            print("\n  Causa: as competências cobrem populações diferentes")
            print(f"  ({so_anterior:,} CNPJs existem só em {anterior}). Com fatias")
            print("  IGUAIS nas duas cargas, isso significa que a fatia não é")
            print("  estável entre competências: o mesmo CNPJ muda de arquivo a")
            print("  cada publicação. Nesse caso não existe atalho — o CDC exige")
            print("  TODAS as 10 fatias nas duas competências.")
        else:
            print("\n  Causa provável: conjuntos de fatias diferentes entre as")
            print("  competências. Confira --fatias em run_pipeline.py.")
        veredito = 2
    else:
        print("VEREDITO: eventos coerentes.")
        print(f"  {dentro/total:.1%} das empresas detectadas abriram dentro da janela")
        print("  esperada — compatível com detecção real de abertura.")
        veredito = 0
    print("=" * 66)

    opp = _tabela(con, "gld_opportunities")
    if opp:
        n = con.execute(f"SELECT count(*) FROM {opp}").fetchone()[0]
        print(f"\nOportunidades no Gold: {n:,}")
        if veredito and n:
            print("  (derivadas dos eventos acima — revise antes de usar com cliente)")

    con.close()
    return veredito


def main() -> None:
    ap = argparse.ArgumentParser(description="Sanidade dos eventos NEW_COMPANY")
    ap.add_argument("--competencia", default=None, help="AAAA-MM (default: a do evento)")
    ap.add_argument("--anterior", default=None, help="AAAA-MM comparada")
    args = ap.parse_args()
    raise SystemExit(diagnosticar(args.competencia, args.anterior))


if __name__ == "__main__":
    main()
