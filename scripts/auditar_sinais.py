"""Audita os sinais do score no Gold atual — para calibrar com dado real.

Mostra quantos candidatos do ICP cada sinal atinge, quantos saíram do digest
por causa dos descontos e quais domínios dispararam o sinal de "contador
atual" por compartilhamento. Só imprime DOMÍNIOS de e-mail, nunca o endereço
completo nem nome de pessoa.

Uso:
    python scripts/auditar_sinais.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import DB_PATH  # noqa: E402


def auditar(con: duckdb.DuckDBPyConnection) -> dict:
    um = lambda sql: con.execute(sql).fetchall()[0][0]  # noqa: E731
    candidatos = um("select count(*) from main_gold.gld_icp_matches")
    no_digest = um("select count(*) from main_gold.gld_opportunities")
    return {
        "candidatos": candidatos,
        "no_digest": no_digest,
        "fora_por_desconto": candidatos - no_digest,
        "contador_atual": um("select count(*) from main_gold.gld_icp_matches "
                             "where sinal_contador_atual"),
        "contador_por_padrao": um("select count(*) from main_gold.gld_icp_matches "
                                  "where contador_por_padrao"),
        "contador_por_compartilhamento": um(
            "select count(*) from main_gold.gld_icp_matches "
            "where contador_por_compartilhamento"),
        "fora_da_regiao": um("select count(*) from main_gold.gld_icp_matches "
                             "where sinal_fora_da_regiao"),
        "filial": um("select count(*) from main_gold.gld_icp_matches "
                     "where sinal_filial"),
        "spe": um("select count(*) from main_gold.gld_icp_matches where sinal_spe"),
        "sem_email_valido": um("select count(*) from main_gold.gld_icp_matches "
                               "where not email_valido"),
        "sem_telefone_valido": um("select count(*) from main_gold.gld_icp_matches "
                                  "where not telefone_valido"),
        "dominios_compartilhados": con.execute("""
            select email_dominio, max(n_empresas_contato) as empresas_na_base,
                   count(*) as oportunidades
            from main_gold.gld_icp_matches
            where contador_por_compartilhamento
            group by 1 order by 3 desc, 2 desc limit 15""").fetchall(),
    }


def main() -> None:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    r = auditar(con)
    con.close()
    print("--- AUDITORIA DOS SINAIS ---")
    print(f"candidatos no ICP ........ {r['candidatos']:>6}")
    print(f"no digest (Gold) ......... {r['no_digest']:>6}")
    print(f"fora por desconto ........ {r['fora_por_desconto']:>6}")
    print("sinais entre os candidatos:")
    for chave, rotulo in [
            ("contador_atual", "provável contador atual"),
            ("contador_por_padrao", "  por padrão no domínio"),
            ("contador_por_compartilhamento", "  por e-mail compartilhado"),
            ("fora_da_regiao", "DDD fora da região"),
            ("filial", "filial"), ("spe", "SPE"),
            ("sem_email_valido", "sem e-mail válido"),
            ("sem_telefone_valido", "sem telefone válido")]:
        pct = 100 * r[chave] / r["candidatos"] if r["candidatos"] else 0
        print(f"  {rotulo:<28} {r[chave]:>6}  ({pct:4.1f}%)")
    if r["dominios_compartilhados"]:
        print("domínios que dispararam por compartilhamento "
              "(confira se são mesmo intermediários):")
        for dom, n_base, n_op in r["dominios_compartilhados"]:
            print(f"  {dom:<32} {n_base:>5} empresas na base · {n_op} no ICP")


if __name__ == "__main__":
    main()
