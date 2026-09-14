"""Gera dados SINTÉTICOS no formato exato da Receita Federal.

Objetivo: permitir desenvolver e testar o pipeline de ponta a ponta sem
depender do download de ~85 GB (e da instabilidade dos espelhos oficiais).

Produz DUAS competências para que o diff (set difference) tenha o que comparar:
a competência mais nova contém empresas que não existem na anterior — são
essas que devem virar eventos NEW_COMPANY.

IMPORTANTE: são dados fictícios, para desenvolvimento. Nunca use um digest
gerado a partir deles com um cliente real.

Uso:
    python scripts/gerar_fixtures.py --anterior 2026-07 --atual 2026-08
"""
from __future__ import annotations

import argparse
import random
import zipfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"

random.seed(42)

# ATENÇÃO: os acentos abaixo NÃO são decoração.
# Os arquivos da Receita são latin-1. Fixtures em ASCII puro são idênticas em
# latin-1 e UTF-8, então um pipeline lendo com o encoding errado passa no CI e
# só quebra com dado real — foi exatamente o que aconteceu (CHANGELOG 2.1.2).
# Mantenha caracteres acentuados aqui.
MUNICIPIOS = [
    ("5403", "VARGINHA"), ("5401", "TRÊS CORAÇÕES"), ("5445", "ELÓI MENDES"),
    ("5297", "POÇOS DE CALDAS"), ("4123", "BELO HORIZONTE"), ("7107", "SÃO PAULO"),
]

# Alguns CNAEs reproduzem DEFEITOS REAIS dos arquivos da Receita:
# descrições contendo ';' e linhas sem aspas. Se o parser não aguentar isso,
# a carga do arquivo inteiro aborta (ver CHANGELOG 2.1.1).
CNAES = [
    ("6920601", "Atividades de contabilidade"),
    ("6920602", "Atividades de consultoria e auditoria contábil e tributária"),
    ("7020400", "Atividades de consultoria em gestão empresarial"),
    ("6201501", "Desenvolvimento de programas de computador sob encomenda"),
    ("5611201", "Restaurantes e similares"),
    ("4781400", "Comércio varejista de artigos do vestuário"),
    ("9602501", "Cabeleireiros, manicure e pedicure"),
    ("4120400", "Construção de edifícios"),
    ("8630501", "Atividade médica ambulatorial"),
    ("4930202", "Transporte rodoviário de carga"),
    # >>> caso patológico 1: ';' dentro da descrição
    ("4618401", "Representantes comerciais; agentes do comércio"),
    # >>> caso patológico 2: aspas duplas dentro da descrição
    ('4530703', 'Comércio a varejo de peças 1" e 2"'),
    # >>> caso patológico 3: descrição acentuada longa (encoding)
    ("0141501", "Produção de sementes certificadas, exceto forrageiras"),
    # >>> caso patológico 4: bytes 0x80–0x9F do Windows-1252 (aspa curva e
    # travessão). Sob latin-1 o DuckDB aborta o arquivo INTEIRO com
    # "File is not latin-1 encoded" — nem ignore_errors salva.
    ("4530703", "Comércio de peças “genuínas” – sob encomenda"),
]

RAZOES = [
    "ALPHA", "BETA", "GAMA", "DELTA", "OMEGA", "SIGMA", "AURORA", "HORIZONTE",
    "PRIMAVERA", "ATLÂNTICO", "PLANALTO", "VÉRTICE", "NOVA ERA", "CONSTRUÇÃO",
]
SUFIXOS = ["LTDA", "ME", "EIRELI", "SOCIEDADE SIMPLES", "S/A"]
PORTES = ["01", "03", "05"]


def _linha_estabelecimento(cnpj_basico: str, cnae: str, municipio: str,
                           uf: str, dt_inicio: str, situacao: str,
                           fantasia: str) -> str:
    cols = [""] * 30
    cols[0] = cnpj_basico
    cols[1] = "0001"
    cols[2] = "99"
    cols[3] = "1"
    cols[4] = fantasia
    cols[5] = situacao
    cols[6] = dt_inicio
    cols[10] = dt_inicio
    cols[11] = cnae
    cols[12] = ""
    cols[13] = "RUA"
    cols[14] = "DAS FLORES"
    cols[15] = str(random.randint(10, 999))
    cols[17] = "CENTRO"
    cols[18] = "37000000"
    cols[19] = uf
    cols[20] = municipio
    cols[21] = "35"
    cols[22] = f"3{random.randint(1000000, 9999999)}"
    cols[27] = f"contato{cnpj_basico[:4]}@exemplo.com.br"
    return ";".join(f'"{c}"' for c in cols)


def _linha_empresa(cnpj_basico: str, razao: str, porte: str, capital: str) -> str:
    cols = [""] * 7
    cols[0] = cnpj_basico
    cols[1] = razao
    cols[2] = "2062"
    cols[3] = "49"
    cols[4] = capital
    cols[5] = porte
    return ";".join(f'"{c}"' for c in cols)


def _zipar(destino: Path, nome_interno: str, linhas: list[str]) -> None:
    # cp1252, e não latin-1: é assim que a Receita realmente grava. Os bytes
    # 0x80–0x9F (aspa curva, travessão) existem no dado real e fazem o leitor
    # do DuckDB abortar se não forem normalizados na extração (CHANGELOG 2.1.3).
    destino.parent.mkdir(parents=True, exist_ok=True)
    conteudo = "\n".join(linhas).encode("cp1252", errors="replace")
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(nome_interno, conteudo)


def gerar(competencia: str, cnpjs: list[dict], ref: date) -> None:
    destino = RAW / competencia
    est, emp = [], []
    for c in cnpjs:
        est.append(_linha_estabelecimento(
            c["cnpj_basico"], c["cnae"], c["municipio"], c["uf"],
            c["dt_inicio"], c["situacao"], c["fantasia"]))
        emp.append(_linha_empresa(
            c["cnpj_basico"], c["razao"], c["porte"], c["capital"]))

    _zipar(destino / "Estabelecimentos0.zip", "K3241.K03200Y0.D60808.ESTABELE", est)
    _zipar(destino / "Empresas0.zip", "K3241.K03200Y0.D60808.EMPRECSV", emp)
    # A Receita não é consistente: parte das linhas vem sem aspas.
    linhas_cnae = []
    for i, (c, d) in enumerate(CNAES):
        if i % 3 == 2:                       # 1 em cada 3 sai SEM aspas
            linhas_cnae.append(f"{c};{d}")
        else:
            linhas_cnae.append(f'"{c}";"{d}"')
    _zipar(destino / "Cnaes.zip", "F.K03200$Z.D60808.CNAECSV", linhas_cnae)
    _zipar(destino / "Municipios.zip", "F.K03200$Z.D60808.MUNICCSV",
           [f'"{c}";"{d}"' for c, d in MUNICIPIOS])
    print(f"  {competencia}: {len(cnpjs)} estabelecimentos, {len(cnpjs)} empresas")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--anterior", default="2026-07")
    ap.add_argument("--atual", default="2026-08")
    ap.add_argument("--base", type=int, default=400, help="empresas na competência anterior")
    ap.add_argument("--novas", type=int, default=60, help="empresas novas na atual")
    args = ap.parse_args()

    hoje = date.today()

    def _fabricar(idx: int, nova: bool) -> dict:
        cnae, _ = random.choice(CNAES)
        mun, _ = random.choice(MUNICIPIOS)
        uf = "SP" if mun == "7107" else "MG"
        if nova:
            dias = random.randint(0, 40)
            dt = (hoje - timedelta(days=dias)).strftime("%Y%m%d")
        else:
            dt = (hoje - timedelta(days=random.randint(400, 4000))).strftime("%Y%m%d")
        razao = f"{random.choice(RAZOES)} {random.choice(RAZOES)} {random.choice(SUFIXOS)}"
        return {
            "cnpj_basico": f"{idx:08d}",
            "cnae": cnae,
            "municipio": mun,
            "uf": uf,
            "dt_inicio": dt,
            # ~8% inativas, para exercitar o filtro de situação cadastral
            "situacao": "08" if random.random() < 0.08 else "02",
            "razao": razao,
            # ~5% dos nomes fantasia carregam um ';' DENTRO do campo (entre
            # aspas), defeito real que fazia o parser enxergar 31 colunas num
            # layout de 30 e abortar a fatia inteira (CHANGELOG 2.1.2).
            "fantasia": (f"{razao.split()[0]}; FILIAL"
                         if random.random() < 0.05 else razao.split()[0]),
            "porte": random.choice(PORTES),
            "capital": str(random.choice([0, 1000, 10000, 50000, 200000])) + ",00",
        }

    print("Gerando fixtures sintéticas (formato real da Receita)...")
    antigas = [_fabricar(i, nova=False) for i in range(1, args.base + 1)]
    gerar(args.anterior, antigas, hoje)

    # A competência atual = as antigas + um bloco de empresas realmente novas.
    novas = [_fabricar(args.base + i, nova=True) for i in range(1, args.novas + 1)]
    gerar(args.atual, antigas + novas, hoje)

    print(f"\nOK. {args.novas} empresas existem apenas em {args.atual} — "
          f"são elas que o diff deve detectar como NEW_COMPANY.")
    print(f"Arquivos em: {RAW}")


if __name__ == "__main__":
    main()
