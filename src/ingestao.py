"""Ingestão Bronze — Dados Abertos do CNPJ.

Mudanças estruturais em relação à versão anterior:
  * COMPETÊNCIA é cidadã de primeira classe. Cada carga grava
    data/bronze/<tabela>/competencia=AAAA-MM/*.parquet. Sem isso não existe
    diff entre meses e o Event Store é impossível.
  * Baixa as 10 fatias de Estabelecimentos e Empresas, UMA DE CADA VEZ:
    baixa, extrai, carrega e apaga o CSV antes de passar à próxima. Carregar
    tudo de uma vez exigiria ~45 GB de CSV extraído por competência; assim o
    pico fica em poucos GB. Cada fatia vira um parquet dentro da partição da
    competência, e o Hive partitioning lê a pasta inteira.
  * Ingere EMPRESAS (razão social, capital, porte).
  * Filtro de UF opcional na carga, para desenvolvimento rápido. ATENÇÃO:
    carga parcial de fatias serve para testar o encanamento, nunca para gerar
    digest — a fatia não é estável entre competências.
  * Verificação de integridade do ZIP.
  * Resiliência de SO: Tratamento de locks de arquivo (ex: OneDrive).
  * Resiliência de Dados: os arquivos da Receita são latin-1, delimitados por
    ';' e com campos entre aspas duplas. Domínios (CNAE/Município) são lidos
    linha a linha para tolerar ';' na descrição; arquivos largos usam
    store_rejects, de modo que linha ruim não derruba a carga nem some em
    silêncio.

Uso:
    python -m src.ingestao --competencia 2024-08 --uf MG
    python -m src.ingestao --competencia 2024-08 --fatias 0,1  # amostra
"""
from __future__ import annotations

import argparse
import os
import shutil
import time
import zipfile
from pathlib import Path

import duckdb
import httpx

from . import observabilidade as obs
from . import receita
from .config import (
    ARQUIVOS_DOMINIO,
    BRONZE_DIR,
    DB_PATH,
    FATIAS,
    LAYOUT_EMPRESAS,
    LAYOUT_ESTABELECIMENTOS,
    N_COLUNAS_EMPRESAS,
    N_COLUNAS_ESTABELECIMENTOS,
    RAW_DIR,
    RFB_DICIONARIO_LAYOUT,
    url_download,
)

# Acima desta fração de linhas rejeitadas, o problema não é "linha ruim" e sim
# "layout mudou" — melhor parar do que ingerir uma base incompleta.
LIMIAR_REJEICAO = 0.01

# A cartilha (§2.1) diz que a fonte é latin-1, e quase é: na prática os
# arquivos trazem bytes 0x80–0x9F, que o latin-1 reserva para controle e o
# Windows-1252 usa para aspas curvas (“ ”), travessão (–) e apóstrofo (’) —
# resíduo de texto digitado no Windows, comum em nome fantasia.
#
# Isso é intratável no leitor do DuckDB, que só aceita utf-8, utf-16 e
# latin-1: sob latin-1 ele aborta com "File is not latin-1 encoded" (e
# ignore_errors NÃO salva — é falha dura, não rejeição de linha), e sob utf-8
# esses bytes chegaram a invalidar a conexão inteira.
#
# Por isso normalizamos na EXTRAÇÃO (ver `extrair`): cp1252 -> UTF-8. Como a
# extração já grava o arquivo, a conversão não custa I/O adicional, e daí para
# frente todo o pipeline lê UTF-8 — inclusive preservando “ ” e – em vez de
# trocá-los por um substituto qualquer.
ENCODING_FONTE = "cp1252"   # como a Receita realmente grava
ENCODING_RFB = "utf-8"      # como gravamos em data/raw/<competencia>/csv/

TIMEOUT = httpx.Timeout(600.0, connect=15.0)
# A cadeia TLS da Receita já causou problemas; permitimos desligar a
# verificação por variável de ambiente, mas NUNCA por padrão.
VERIFICAR_TLS = os.environ.get("RADAR_TLS_INSECURE", "0") != "1"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


# ---------------------------------------------------------------- download
def _baixar(url: str, destino: Path, tentativas: int = 3) -> str:
    """Baixa uma URL. Retorna OK | NOT_FOUND | FALHOU."""
    tmp = destino.with_suffix(destino.suffix + ".part")
    for n in range(1, tentativas + 1):
        try:
            with httpx.stream("GET", url, headers=HEADERS, timeout=TIMEOUT,
                              verify=VERIFICAR_TLS, follow_redirects=True) as r:
                if r.status_code == 404:
                    return "NOT_FOUND"
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_bytes(chunk_size=1 << 16):
                        f.write(chunk)
            tmp.replace(destino)
            return "OK"
        except (httpx.ConnectTimeout, httpx.ConnectError) as e:
            print(f"    tentativa {n}: sem conexão ({type(e).__name__})")
        except Exception as e:  # noqa: BLE001 - queremos continuar tentando
            print(f"    tentativa {n}: {type(e).__name__}: {e}")
        if n < tentativas:
            time.sleep(min(10 * 2 ** (n - 1), 60))
    if tmp.exists():
        tmp.unlink()
    return "FALHOU"


def _zip_integro(caminho: Path) -> bool:
    """Um ZIP truncado passava como válido só por existir. Agora validamos."""
    if not caminho.exists() or caminho.stat().st_size == 0:
        return False
    try:
        with zipfile.ZipFile(caminho) as z:
            return z.testzip() is None and len(z.namelist()) > 0
    except zipfile.BadZipFile:
        return False


def baixar_arquivo(nome_arquivo: str, competencia: str, destino_dir: Path) -> Path:
    """Baixa um arquivo da competência informada do share WebDAV da RFB."""
    destino_dir.mkdir(parents=True, exist_ok=True)
    destino = destino_dir / nome_arquivo

    if _zip_integro(destino):
        print(f"  [SKIP] {nome_arquivo} já presente e íntegro.")
        return destino
    if destino.exists():
        print(f"  [REBAIXAR] {nome_arquivo} existe mas está corrompido/truncado.")
        destino.unlink()

    url = url_download(nome_arquivo, competencia)
    print(f"  [GET] {nome_arquivo} ({competencia})")
    status = _baixar(url, destino, tentativas=4)

    if status == "OK" and _zip_integro(destino):
        print(f"  [OK] {nome_arquivo}")
        return destino
    destino.unlink(missing_ok=True)

    if status == "NOT_FOUND":
        raise RuntimeError(
            f"{nome_arquivo} não existe na competência {competencia}.\n"
            f"Use --listar para ver o que está publicado."
        )
    raise RuntimeError(
        f"Falha ao baixar {nome_arquivo} da competência {competencia}. "
        f"A infraestrutura da RFB é instável; tente novamente mais tarde."
    )


def extrair(zip_path: Path, destino_dir: Path) -> list[Path]:
    """Extrai TODOS os membros do ZIP, normalizando o encoding para UTF-8.

    A conversão cp1252 -> UTF-8 acontece aqui, em streaming, porque a extração
    já precisa escrever o arquivo: é o único ponto do pipeline onde normalizar
    não custa uma passada extra de I/O sobre gigabytes.

    cp1252 é single-byte, então nenhum caractere atravessa a fronteira entre
    dois blocos — dá para converter em pedaços sem risco de partir uma
    sequência no meio. Os 5 bytes que o cp1252 não define (0x81, 0x8D, 0x8F,
    0x90, 0x9D) viram U+FFFD; não ocorrem em dado real da Receita, e é melhor
    marcar o byte do que abortar a carga.
    """
    destino_dir.mkdir(parents=True, exist_ok=True)
    saidas = []
    with zipfile.ZipFile(zip_path) as z:
        for membro in z.namelist():
            if membro.endswith("/"):
                continue
            destino = destino_dir / Path(membro).name
            with z.open(membro) as origem, open(destino, "wb") as saida:
                while bloco := origem.read(1 << 20):
                    saida.write(bloco.decode(ENCODING_FONTE, errors="replace")
                                     .encode(ENCODING_RFB))
            saidas.append(destino)
    return saidas


# ------------------------------------------------------------------- carga
def _select_colunas(layout: dict[str, int]) -> str:
    """Monta o SELECT posicional a partir do layout oficial."""
    return ",\n        ".join(
        f"column{idx:02d} AS {nome}" for nome, idx in layout.items()
    )


def _remover_diretorio_seguro(caminho: Path):
    """Remove diretório com resiliência contra locks temporários do SO/OneDrive."""
    if caminho.exists():
        try:
            shutil.rmtree(caminho)
        except PermissionError:
            print(f"  [AVISO] Lock de arquivo detectado em {caminho.name}. Aguardando liberação...")
            time.sleep(2) # Espera o processo fantasma soltar o arquivo
            shutil.rmtree(caminho, ignore_errors=True)


def _contar_linhas_cruas(con, csvs: list[Path]) -> int:
    """Conta as linhas físicas dos arquivos, sem interpretar campos.

    Usado só como denominador do limiar de rejeição. Lê cada linha como um
    campo único (delimitador \\x07, que não ocorre nos arquivos da Receita),
    então nenhuma linha é descartada por defeito de formato.
    """
    lista = ", ".join(f"'{p.as_posix()}'" for p in csvs)
    return con.execute(f"""
        SELECT count(*) FROM read_csv([{lista}], auto_detect=false,
                                      delim='\\x07', header=false,
                                      quote='', escape='',
                                      encoding='{ENCODING_RFB}',
                                      all_varchar=true,
                                      columns={{'linha': 'VARCHAR'}})
    """).fetchone()[0]


def _carregar_parquet(con, tabela: str, csvs: list[Path], layout: dict[str, int],
                      n_colunas: int, competencia: str, uf: str | None,
                      fatia: int | None = None, limpar: bool = True) -> tuple[int, int]:
    """Lê os CSVs crus e grava (parte d)a partição Parquet da competência.

    `fatia` grava um arquivo próprio dentro da partição (`dados_3.parquet`),
    permitindo carregar uma fatia por vez e apagar o CSV antes de baixar a
    seguinte. Hive partitioning lê todos os parquet da pasta, então o
    resultado é idêntico ao de uma carga única — com pico de disco de poucos
    GB em vez de mais de 100.

    `limpar=False` preserva o que já foi gravado na partição: só a primeira
    fatia zera a pasta.
    """
    if not csvs:
        raise RuntimeError(f"Nenhum CSV encontrado para {tabela}")

    saida = BRONZE_DIR / tabela / f"competencia={competencia}"
    if limpar:
        _remover_diretorio_seguro(saida)
    saida.mkdir(parents=True, exist_ok=True)
    destino = saida / (f"dados_{fatia}.parquet" if fatia is not None else "dados.parquet")

    lista = ", ".join(f"'{p.as_posix()}'" for p in csvs)
    filtro_uf = ""
    if uf and "uf" in layout:
        filtro_uf = f"WHERE column{layout['uf']:02d} = '{uf}'"

    # Os arquivos da Receita são latin-1, delimitados por ';' e com os campos
    # entre aspas duplas (cartilha §2.1, "pegadinhas da fonte"). Declarar
    # quote='"' é o que faz um ';' DENTRO de um campo ("LOJA A; LOJA B") não
    # virar uma coluna extra — e é o que garante que o valor chegue sem as
    # aspas, sem o que o filtro de UF jamais casaria.
    # ignore_errors tolera a linha ruim; store_rejects impede que ela suma em
    # silêncio, alimentando o limiar de rejeição logo abaixo.
    #
    # auto_detect=false porque já declaramos delimitador, aspas, encoding e
    # colunas: sem isso o sniffer do DuckDB ainda tenta deduzir o dialeto e
    # aborta o arquivo INTEIRO ("Error when sniffing file") quando as linhas
    # ruins confundem a amostra — justamente o que precisamos tolerar.
    # store_rejects materializa DUAS tabelas: a de erros e a de scans. Se só
    # nomearmos a primeira, a segunda colide com o nome padrão na segunda
    # chamada da mesma conexão ("reject_scans is already in use").
    sufixo = tabela if fatia is None else f"{tabela}_{fatia}"
    tabela_rejeicoes = f"rejeicoes_{sufixo}"
    tabela_scans = f"scans_{sufixo}"
    con.execute(f"DROP TABLE IF EXISTS {tabela_rejeicoes}")
    con.execute(f"DROP TABLE IF EXISTS {tabela_scans}")
    con.execute(f"""
        COPY (
            SELECT {_select_colunas(layout)},
                   '{competencia}' AS competencia
            FROM read_csv([{lista}],
                          auto_detect=false,
                          delim=';', header=false, quote='"',
                          encoding='{ENCODING_RFB}',
                          all_varchar=true,
                          ignore_errors=true, store_rejects=true,
                          rejects_table='{tabela_rejeicoes}',
                          rejects_scan='{tabela_scans}',
                          columns={{{', '.join(f"'column{i:02d}': 'VARCHAR'" for i in range(n_colunas))}}})
            {filtro_uf}
        ) TO '{destino.as_posix()}' (FORMAT PARQUET)
    """)

    inseridas = con.execute(
        f"SELECT count(*) FROM read_parquet('{destino.as_posix()}')"
    ).fetchone()[0]

    # Uma linha malformada gera VÁRIOS registros de erro (um por coluna
    # excedente). Contar registros superestimaria a rejeição e dispararia o
    # limiar sem motivo — o que interessa é quantas LINHAS se perderam.
    rejeitadas = con.execute(
        f"SELECT count(DISTINCT (file_id, line)) FROM {tabela_rejeicoes}"
    ).fetchone()[0] if _tabela_existe(con, tabela_rejeicoes) else 0

    return inseridas, rejeitadas


def _tabela_existe(con, nome: str) -> bool:
    """DuckDB só materializa a tabela de rejeições se houve alguma rejeição."""
    return con.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE table_name = ?", [nome]
    ).fetchone()[0] > 0


def _carregar_dominio(con, tabela: str, csvs: list[Path], competencia: str) -> int:
    """Tabelas de domínio (CNAE, Município): sempre 2 colunas id;descrição.

    Estes arquivos são os mais sujos da Receita: parte das linhas vem sem
    aspas e várias descrições contêm ';' ("Representantes comerciais; agentes
    do comercio"). Qualquer parser de CSV convencional enxerga 3 campos e
    aborta o arquivo inteiro.

    Solução (CHANGELOG 2.1.1): lemos a LINHA INTEIRA como campo único —
    usando \\x07 como delimitador, byte que não ocorre nos arquivos — e
    partimos no PRIMEIRO ';'. O código nunca contém ';', logo tudo à direita
    é a descrição, não importa quantos ';' ela tenha nem se está entre aspas.
    Zero linhas perdidas, por construção.

    Deliberadamente NÃO usamos ignore_errors aqui: perder um CNAE em silêncio
    significa empresas deixando de casar com o ICP.
    """
    saida = BRONZE_DIR / tabela / f"competencia={competencia}"
    _remover_diretorio_seguro(saida)
    saida.mkdir(parents=True, exist_ok=True)
    destino = saida / "dados.parquet"
    lista = ", ".join(f"'{p.as_posix()}'" for p in csvs)

    con.execute(f"""
        COPY (
            SELECT trim(split_part(linha, ';', 1), '"') AS codigo,
                   trim(substr(linha, strpos(linha, ';') + 1), '"') AS descricao,
                   '{competencia}' AS competencia
            FROM read_csv([{lista}], auto_detect=false,
                          delim='\\x07', header=false, quote='', escape='',
                          encoding='{ENCODING_RFB}', all_varchar=true,
                          columns={{'linha':'VARCHAR'}})
            WHERE strpos(linha, ';') > 0
        ) TO '{destino.as_posix()}' (FORMAT PARQUET)
    """)
    return con.execute(
        f"SELECT count(*) FROM read_parquet('{(saida / 'dados.parquet').as_posix()}')"
    ).fetchone()[0]


def ingerir(competencia: str, uf: str | None = None,
            fatias: list[int] | None = None, somente_local: bool = False) -> dict:
    """Executa a ingestão completa de uma competência."""
    fatias = list(FATIAS) if fatias is None else fatias
    raw = RAW_DIR / competencia
    print(f"\n=== INGESTÃO BRONZE — competência {competencia} ===")
    if uf:
        print(f"    filtro de UF: {uf}")

    if sorted(fatias) != sorted(FATIAS):
        print(f"\n  [ATENÇÃO] Carga parcial: fatias {sorted(fatias)} de "
              f"{sorted(FATIAS)}.")
        print("  A fatia NÃO é estável entre competências — o mesmo CNPJ muda")
        print("  de arquivo a cada publicação da Receita. Duas competências")
        print("  ingeridas com a mesma fatia cobrem populações DIFERENTES, e o")
        print("  set difference passa a medir sorteio em vez de abertura de")
        print("  empresa. Serve para testar o encanamento; NÃO serve para")
        print("  gerar digest. Confira com scripts/diagnostico_eventos.py.")

    # ------------------------------------------------------- domínios
    dominio: dict[str, Path] = {}
    if not somente_local:
        for nome, arq in ARQUIVOS_DOMINIO.items():
            dominio[nome] = baixar_arquivo(arq, competencia, raw)
    else:
        for nome, arq in ARQUIVOS_DOMINIO.items():
            p = raw / arq
            if p.exists():
                dominio[nome] = p

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    contagens: dict[str, int] = {}
    rejeitadas: dict[str, int] = {"estabelecimentos": 0, "empresas": 0}

    print("\n[CARGA -> Parquet particionado]")
    for nome, z in dominio.items():
        csvs_dominio = extrair(z, raw / "csv")
        contagens[nome] = _carregar_dominio(con, nome, csvs_dominio, competencia)
        for c in csvs_dominio:
            c.unlink(missing_ok=True)

    # --------------------------------------------- fatias, uma de cada vez
    #
    # Baixa -> extrai -> carrega -> APAGA o CSV, e só então passa à próxima.
    # Carregar as 10 fatias de uma vez exigiria manter ~45 GB de CSV extraído
    # por competência no disco; assim o pico fica em poucos GB. Os ZIPs ficam,
    # porque são o cache que evita rebaixar tudo (o [SKIP] depende deles).
    tabelas = (
        ("estabelecimentos", "Estabelecimentos",
         LAYOUT_ESTABELECIMENTOS, N_COLUNAS_ESTABELECIMENTOS, uf),
        ("empresas", "Empresas",
         LAYOUT_EMPRESAS, N_COLUNAS_EMPRESAS, None),
    )

    try:
        for posicao, i in enumerate(fatias):
            for tabela, prefixo, layout, n_col, filtro in tabelas:
                nome_zip = f"{prefixo}{i}.zip"
                if somente_local:
                    caminho_zip = raw / nome_zip
                    if not caminho_zip.exists():
                        print(f"  [PULA] {nome_zip} ausente (modo offline).")
                        continue
                else:
                    caminho_zip = baixar_arquivo(nome_zip, competencia, raw)

                csvs_fatia = extrair(caminho_zip, raw / "csv")
                try:
                    ins, rej = _carregar_parquet(
                        con, tabela, csvs_fatia, layout, n_col, competencia,
                        filtro, fatia=i, limpar=(posicao == 0))
                    contagens[tabela] = contagens.get(tabela, 0) + ins
                    rejeitadas[tabela] += rej
                    print(f"    fatia {i} · {tabela:18s} {ins:>12,} linhas"
                          + (f"  ({rej:,} rejeitada(s))" if rej else ""))

                    # Tolerar linha ruim é aceitável; tolerar arquivo ruim não
                    # é. O denominador tem de ser o total de linhas do ARQUIVO:
                    # com --uf MG, "carregadas" conta só Minas enquanto as
                    # rejeições vêm do país inteiro. Só pagamos essa contagem
                    # quando houve rejeição — e aqui, enquanto o CSV existe.
                    if rej:
                        total = _contar_linhas_cruas(con, csvs_fatia)
                        if total and rej / total > LIMIAR_REJEICAO:
                            raise RuntimeError(
                                f"{tabela} (fatia {i}): {rej:,} de {total:,} "
                                f"linhas rejeitadas (>{LIMIAR_REJEICAO:.1%}). O "
                                f"layout da Receita pode ter mudado — confira o "
                                f"dicionário em {RFB_DICIONARIO_LAYOUT}")
                finally:
                    for c in csvs_fatia:
                        c.unlink(missing_ok=True)
    finally:
        con.close()

    print()
    for nome, n in contagens.items():
        rej = rejeitadas.get(nome, 0)
        extra = f"  ({rej:,} linha(s) rejeitada(s))" if rej else ""
        print(f"    {nome:20s} {n:>12,} linhas{extra}")

    contagens["_rejeitadas"] = sum(rejeitadas.values())
    return contagens


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingestão Bronze do Radar B2B")
    ap.add_argument("--competencia", default=None,
                    help="AAAA-MM (default: última publicada pela Receita)")
    ap.add_argument("--uf", default=None, help="filtra estabelecimentos por UF")
    ap.add_argument("--fatias", default=None,
                    help="fatias a baixar, ex: 0,1 (default: todas)")
    ap.add_argument("--somente-local", action="store_true",
                    help="não baixa; usa os ZIPs já presentes em data/raw")
    ap.add_argument("--listar", action="store_true",
                    help="lista as competências publicadas e sai")
    args = ap.parse_args()

    if args.listar:
        comps = receita.listar_competencias()
        print("Competências publicadas pela Receita Federal:")
        for c in comps:
            print(f"  {c}" + ("   <- mais recente" if c == comps[-1] else ""))
        print(f"\nArquivos em {comps[-1]}:")
        for a in receita.listar_arquivos(comps[-1]):
            print(f"  {a}")
        return

    competencia = args.competencia
    if competencia is None:
        if args.somente_local:
            raise SystemExit("--somente-local exige --competencia explícita.")
        competencia = receita.ultima_competencia()
        print(f"[INFO] Competência não informada; usando a mais recente "
              f"publicada: {competencia}")

    fatias = ([int(x) for x in args.fatias.split(",")] if args.fatias else None)

    with obs.etapa("ingestao_bronze", competencia) as ctx:
        contagens = ingerir(competencia, args.uf, fatias, args.somente_local)
        ctx["registros"] = contagens.get("estabelecimentos", 0)
        ctx["detalhe"] = str(contagens)
    print("\n--- INGESTÃO CONCLUÍDA ---")


if __name__ == "__main__":
    main()
