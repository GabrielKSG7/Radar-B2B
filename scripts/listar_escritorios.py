"""Lista escritórios de contabilidade da região para o convite ao piloto.

Fonte: a própria base pública da Receita já carregada no Radar (Silver).
Só entram PESSOAS JURÍDICAS (empresário individual/MEI ficam de fora: o
telefone e o e-mail costumam ser da pessoa física) e estabelecimentos ativos.

A ordem é uma heurística de facilidade de contato, NÃO uma probabilidade de
resposta: matriz, tempo de atividade, telefone, e-mail de domínio próprio.
Revise a lista à mão antes de qualquer envio.

Uso:
    python scripts/listar_escritorios.py
    python scripts/listar_escritorios.py --municipios "VARGINHA,POUSO ALEGRE" --limite 30
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import DB_PATH, DIGEST_DIR  # noqa: E402
from src.privacidade import NATUREZAS_INDIVIDUAIS, PROVEDORES_PESSOAIS  # noqa: E402

CNAES_CONTABILIDADE = ("6920601", "6920602")
MUNICIPIOS_PADRAO = ("VARGINHA", "TRES CORACOES", "ELOI MENDES")

SQL = """
with comp as (select max(competencia) as c from main_silver.slv_estabelecimentos)
select
    e.cnpj, emp.razao_social, e.nome_fantasia, mun.descricao as municipio,
    e.bairro, e.telefone, e.email, e.data_inicio_atividade,
    e.identificador_matriz_filial, emp.natureza_juridica, comp.c as competencia
from main_silver.slv_estabelecimentos e
cross join comp
join main_silver.slv_empresas emp
  on emp.cnpj_basico = e.cnpj_basico and emp.competencia = e.competencia
left join main_silver.slv_dominios mun
  on mun.dominio = 'MUNICIPIO' and mun.codigo = e.id_municipio
where e.competencia = comp.c
  and e.situacao_cadastral = '02'
  and e.uf = 'MG'
  and e.cnae_principal in ({cnaes})
  and mun.descricao in ({municipios})
  and coalesce(emp.natureza_juridica, '') not in ({naturezas})
"""


def _marcadores(n: int) -> str:
    return ", ".join("?" for _ in range(n))


def _dominio(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return email.rsplit("@", 1)[1].strip().lower()


def pontuar(r: dict, hoje: date) -> tuple[int, str]:
    """Facilidade de contato, 0-100, com os fatores em texto."""
    pontos, fatores = 0, []
    if str(r["identificador_matriz_filial"]) == "1":
        pontos += 25
        fatores.append("matriz")
    inicio = r["data_inicio_atividade"]
    if inicio and (hoje - inicio).days >= 730:
        pontos += 20
        fatores.append("mais de 2 anos de atividade")
    if r["telefone"]:
        pontos += 25
        fatores.append("telefone")
    dom = _dominio(r["email"])
    if dom and dom not in PROVEDORES_PESSOAIS:
        pontos += 30
        fatores.append("e-mail de domínio próprio")
    elif dom:
        pontos += 10
        fatores.append("e-mail de provedor gratuito")
    return pontos, " · ".join(fatores) or "sem contato público"


def listar(con: duckdb.DuckDBPyConnection, municipios, limite: int, hoje: date) -> list[dict]:
    sql = SQL.format(
        cnaes=_marcadores(len(CNAES_CONTABILIDADE)),
        municipios=_marcadores(len(municipios)),
        naturezas=_marcadores(len(NATUREZAS_INDIVIDUAIS)),
    )
    params = [*CNAES_CONTABILIDADE, *municipios, *sorted(NATUREZAS_INDIVIDUAIS)]
    cur = con.execute(sql, params)
    cols = [c[0] for c in cur.description]
    linhas = [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
    for r in linhas:
        r["pontos"], r["fatores"] = pontuar(r, hoje)
    linhas.sort(key=lambda r: (-r["pontos"], r["razao_social"] or ""))
    return linhas[:limite]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--municipios", default=",".join(MUNICIPIOS_PADRAO),
                    help="nomes em maiúsculas e sem acento, separados por vírgula")
    ap.add_argument("--limite", type=int, default=25)
    args = ap.parse_args()
    municipios = [m.strip().upper() for m in args.municipios.split(",") if m.strip()]

    con = duckdb.connect(str(DB_PATH), read_only=True)
    linhas = listar(con, municipios, args.limite, date.today())
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    saida = DIGEST_DIR / f"escritorios_candidatos_{date.today().isoformat()}.csv"
    campos = ["pontos", "razao_social", "nome_fantasia", "municipio", "bairro",
              "telefone", "email", "fatores", "cnpj", "contato_feito",
              "respondeu", "observacoes"]
    with open(saida, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=campos, delimiter=";", extrasaction="ignore")
        w.writeheader()
        for r in linhas:
            w.writerow({**r, "contato_feito": "", "respondeu": "", "observacoes": ""})
    print(f"[OK] {len(linhas)} escritório(s) em {saida}")


if __name__ == "__main__":
    main()
