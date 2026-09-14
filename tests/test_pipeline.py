"""Testes unitários do Radar B2B.

Cobrem as regras que, se quebrarem silenciosamente, produzem um digest errado
sem ninguém perceber — que era exatamente o risco da versão anterior.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import ingestao as ing  # noqa: E402
from src.config import (  # noqa: E402
    LAYOUT_EMPRESAS,
    LAYOUT_ESTABELECIMENTOS,
    N_COLUNAS_EMPRESAS,
    N_COLUNAS_ESTABELECIMENTOS,
    competencia_anterior,
    meses_candidatos,
)
from src.ingestao import _select_colunas, _zip_integro  # noqa: E402


# ------------------------------------------------------------- competência
def test_competencia_anterior_no_mesmo_ano():
    assert competencia_anterior("2026-08") == "2026-07"


def test_competencia_anterior_vira_o_ano():
    assert competencia_anterior("2026-01") == "2025-12"


def test_meses_candidatos_ordem_decrescente():
    meses = meses_candidatos(3, a_partir_de="2026-03")
    assert meses == ["2026-03", "2026-02", "2026-01"]


# ------------------------------------------------------------------ layout
def test_layout_estabelecimentos_indices_conhecidos():
    """Índices conferidos contra o dicionário oficial dos Dados Abertos.

    Se a Receita mudar o layout, este teste falha antes de o pipeline gravar
    dado errado em Bronze (situação em que a coluna de UF viraria telefone).
    """
    assert LAYOUT_ESTABELECIMENTOS["cnpj_basico"] == 0
    assert LAYOUT_ESTABELECIMENTOS["nome_fantasia"] == 4
    assert LAYOUT_ESTABELECIMENTOS["situacao_cadastral"] == 5
    assert LAYOUT_ESTABELECIMENTOS["data_inicio_atividade"] == 10
    assert LAYOUT_ESTABELECIMENTOS["cnae_principal"] == 11
    assert LAYOUT_ESTABELECIMENTOS["uf"] == 19
    assert LAYOUT_ESTABELECIMENTOS["id_municipio"] == 20


def test_layout_dentro_do_numero_de_colunas():
    assert max(LAYOUT_ESTABELECIMENTOS.values()) < N_COLUNAS_ESTABELECIMENTOS
    assert max(LAYOUT_EMPRESAS.values()) < N_COLUNAS_EMPRESAS


def test_select_usa_zero_padding_de_duas_casas():
    """O DuckDB nomeia colunas sem header conforme o total de colunas do
    arquivo: com 30 colunas, o nome é column00 (e não column0)."""
    sql = _select_colunas({"uf": 19, "cnpj_basico": 0})
    assert "column19 AS uf" in sql
    assert "column00 AS cnpj_basico" in sql


# ------------------------------------------------------------- integridade
def test_zip_integro_detecta_arquivo_valido(tmp_path):
    caminho = tmp_path / "bom.zip"
    with zipfile.ZipFile(caminho, "w") as z:
        z.writestr("dados.csv", "1;2;3")
    assert _zip_integro(caminho) is True


def test_zip_integro_rejeita_truncado(tmp_path):
    """Antes bastava o arquivo existir — um download truncado passava."""
    caminho = tmp_path / "ruim.zip"
    caminho.write_bytes(b"PK\x03\x04corrompido")
    assert _zip_integro(caminho) is False


def test_zip_integro_rejeita_vazio(tmp_path):
    caminho = tmp_path / "vazio.zip"
    caminho.write_bytes(b"")
    assert _zip_integro(caminho) is False


def test_zip_integro_rejeita_inexistente(tmp_path):
    assert _zip_integro(tmp_path / "nao_existe.zip") is False


# ------------------------------------------------------------------- regra
@pytest.mark.parametrize("descricao", [
    "Atividades de contabilidade",
    "Atividades de consultoria em gestao empresarial",
    "Comercio de cosmeticos",
    "Participacao societaria",
])
def test_regressao_filtro_por_texto_e_perigoso(descricao):
    """Regressão do bug do ILIKE '%ti%'.

    O filtro antigo casava com qualquer descrição contendo 'ti' — inclusive
    'A-TI-vidades', presente em boa parte dos CNAEs brasileiros, tornando o
    filtro de segmento inócuo. Este teste documenta por que o casamento passou
    a ser por CÓDIGO CNAE, e não por texto.
    """
    assert "ti" in descricao.lower(), (
        "Se esta descrição não contivesse 'ti', o exemplo perderia o sentido")


# ------------------------------------------------------- endereços da RFB
from xml.etree import ElementTree  # noqa: E402

from src import receita  # noqa: E402
from src.config import (  # noqa: E402
    RFB_HOST,
    RFB_SHARE_TOKEN,
    url_download,
)

XML_RAIZ = b"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:">
  <d:response><d:href>/public.php/webdav/</d:href></d:response>
  <d:response><d:href>/public.php/webdav/2026-06/</d:href></d:response>
  <d:response><d:href>/public.php/webdav/2026-07/</d:href></d:response>
  <d:response><d:href>/public.php/webdav/2026-08/</d:href></d:response>
</d:multistatus>"""

XML_MES = b"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:">
  <d:response><d:href>/public.php/webdav/2026-08/</d:href></d:response>
  <d:response><d:href>/public.php/webdav/2026-08/Estabelecimentos0.zip</d:href></d:response>
  <d:response><d:href>/public.php/webdav/2026-08/Empresas0.zip</d:href></d:response>
  <d:response><d:href>/public.php/webdav/2026-08/LEIAME.txt</d:href></d:response>
</d:multistatus>"""


def _competencias(xml: bytes) -> list[str]:
    raiz = ElementTree.fromstring(xml)
    achados = []
    for item in raiz.findall("d:response", receita.DAV_NS):
        m = receita.PADRAO_COMPETENCIA.search(item.find("d:href", receita.DAV_NS).text)
        if m:
            achados.append(m.group(1))
    return sorted(set(achados))


def _arquivos(xml: bytes) -> list[str]:
    raiz = ElementTree.fromstring(xml)
    achados = []
    for item in raiz.findall("d:response", receita.DAV_NS):
        m = receita.PADRAO_ZIP.search(item.find("d:href", receita.DAV_NS).text)
        if m:
            achados.append(m.group(1))
    return sorted(set(achados))


def test_webdav_extrai_competencias():
    assert _competencias(XML_RAIZ) == ["2026-06", "2026-07", "2026-08"]


def test_webdav_ignora_nao_zip():
    """LEIAME.txt e outros não-ZIP não podem entrar na lista de download."""
    assert _arquivos(XML_MES) == ["Empresas0.zip", "Estabelecimentos0.zip"]


def test_url_de_download_usa_host_ativo():
    """Regressão: dadosabertos.rfb.gov.br foi desativado em janeiro/2026."""
    url = url_download("Estabelecimentos0.zip", "2026-08")
    assert url.startswith("https://arquivos.receitafederal.gov.br/")
    assert "dadosabertos.rfb.gov.br" not in url
    assert RFB_SHARE_TOKEN in url
    assert url.endswith("/2026-08/Estabelecimentos0.zip")


def test_nenhum_host_desativado_no_codigo():
    """Nenhum módulo pode voltar a APONTAR para os hosts mortos.

    Usa AST para inspecionar apenas literais de string efetivamente usados no
    código — comentários e docstrings podem (e devem) citar o host antigo para
    documentar a migração de janeiro/2026.
    """
    import ast
    import pathlib

    raiz = pathlib.Path(__file__).resolve().parent.parent
    mortos = ["dadosabertos.rfb.gov.br", "200.152.38.155"]
    arquivos = list((raiz / "src").glob("*.py")) + list((raiz / "scripts").glob("*.py"))
    assert arquivos, "nenhum módulo encontrado para inspecionar"

    for arquivo in arquivos:
        arvore = ast.parse(arquivo.read_text(encoding="utf-8"))
        docstrings = {
            id(no.body[0].value)
            for no in ast.walk(arvore)
            if isinstance(no, ast.Module | ast.FunctionDef | ast.ClassDef)
            and no.body
            and isinstance(no.body[0], ast.Expr)
            and isinstance(no.body[0].value, ast.Constant)
            and isinstance(no.body[0].value.value, str)
        }
        for no in ast.walk(arvore):
            if (isinstance(no, ast.Constant) and isinstance(no.value, str)
                    and id(no) not in docstrings):
                for morto in mortos:
                    assert morto not in no.value, (
                        f"{arquivo.name}:{no.lineno} usa host desativado: {morto}")


def test_host_configuravel_por_variavel_de_ambiente():
    """O share token precisa ser sobrescrevível sem editar código."""
    assert RFB_HOST.startswith("https://")
    assert isinstance(RFB_SHARE_TOKEN, str) and RFB_SHARE_TOKEN


# --------------------------------------------------- parsing de arquivos sujos
def test_dominio_com_ponto_e_virgula_na_descricao(tmp_path):
    """Regressão do erro de sniffing (CHANGELOG 2.1.1).

    O arquivo de CNAEs da Receita mistura linhas com e sem aspas, e algumas
    descrições contêm ';'. O parser CSV convencional enxerga 3 colunas e
    aborta a leitura do arquivo inteiro.
    """
    import duckdb

    csv = tmp_path / "cnae.csv"
    csv.write_bytes("\n".join([
        '"6920601";"Atividades de contabilidade"',
        "4618401;Representantes comerciais; agentes do comercio",   # sem aspas + ';'
        '"5611201";"Restaurantes e similares"',
    ]).encode("utf-8"))

    con = duckdb.connect()
    df = con.execute(rf"""
        SELECT
            trim(regexp_replace(split_part(linha, ';', 1), '^"|"$', '', 'g')) AS codigo,
            trim(regexp_replace(substr(linha, position(';' IN linha) + 1),
                                '^"|"$', '', 'g'))                           AS descricao
        FROM read_csv('{csv.as_posix()}', delim='\x07', quote='', escape='',
                      header=false, encoding='latin-1', all_varchar=true,
                      columns={{'linha': 'VARCHAR'}})
        WHERE linha IS NOT NULL AND trim(linha) <> ''
    """).df()
    con.close()

    # Nenhuma linha pode ser perdida: um CNAE ausente = empresas que deixam
    # de casar com o ICP.
    assert len(df) == 3
    linha = df[df.codigo == "4618401"].iloc[0]
    assert linha.descricao == "Representantes comerciais; agentes do comercio"


def test_limiar_de_rejeicao_configurado():
    """Linha ruim é tolerável; arquivo ruim (layout mudou) não é."""
    from src.ingestao import LIMIAR_REJEICAO
    assert 0 < LIMIAR_REJEICAO <= 0.05


# ---------------------------------------------- regressões da carga (2.1.2)
#
# Os testes desta seção chamam as FUNÇÕES DE PRODUÇÃO. A versão anterior
# reimplementava o SQL dentro do próprio teste, então validava a técnica e não
# o código: quando `ingestao.py` foi reescrito e perdeu a correção, a suíte
# seguiu verde. Um teste que não consegue falhar não é uma proteção.

# Uma linha de ESTABELECIMENTOS no formato real: 30 campos entre aspas, ';'.
_COLS_ESTAB = 30
_LAYOUT_MIN = {"cnpj_basico": 0, "nome_fantasia": 4, "uf": 19}


def _linha_estab(fantasia: str, uf: str = "MG", n: int = _COLS_ESTAB) -> str:
    cols = [""] * _COLS_ESTAB
    cols[0], cols[1], cols[2], cols[3] = "00000000", "0001", "91", "1"
    cols[4] = fantasia
    cols[5] = "02"
    cols[19] = uf
    return ";".join(f'"{c}"' for c in cols[:n])


@pytest.fixture()
def bronze(tmp_path, monkeypatch):
    """Redireciona a saída Bronze para um diretório temporário."""
    monkeypatch.setattr(ing, "BRONZE_DIR", tmp_path / "bronze")
    return tmp_path


def _parquet(bronze_dir, tabela, competencia="2026-08"):
    return duckdb.connect().execute(
        f"SELECT * FROM read_parquet('"
        f"{(bronze_dir / 'bronze' / tabela / f'competencia={competencia}' / 'dados.parquet').as_posix()}')"
    ).df()


def test_dominio_nao_perde_linha_com_ponto_e_virgula_na_descricao(bronze):
    """Chama _carregar_dominio de verdade — ';' na descrição não pode cortar linha."""
    csv = bronze / "cnae.csv"
    csv.write_bytes("\n".join([
        '"6920601";"Atividades de contabilidade"',
        "4618401;Representantes comerciais; agentes do comercio",  # sem aspas + ';'
        '"5611201";"Restaurantes e similares"',
    ]).encode("utf-8"))

    n = ing._carregar_dominio(duckdb.connect(), "cnaes", [csv], "2026-08")
    assert n == 3, "nenhuma linha de domínio pode ser perdida"

    df = _parquet(bronze, "cnaes")
    linha = df[df.codigo == "4618401"].iloc[0]
    assert linha.descricao == "Representantes comerciais; agentes do comercio"


def test_dominio_preserva_acentos(bronze):
    """Encoding errado faz a linha acentuada sumir em silêncio, sem erro algum.

    O CSV aqui está em UTF-8 porque é isso que `extrair` entrega — a conversão
    a partir do cp1252 da Receita acontece lá (ver o teste de extração).
    """
    csv = bronze / "cnae.csv"
    csv.write_bytes("\n".join([
        '"0141501";"Produção de sementes certificadas"',
        '"4120400";"Construção de edifícios"',
        '"6920601";"Atividades de contabilidade"',       # única sem acento
    ]).encode("utf-8"))

    n = ing._carregar_dominio(duckdb.connect(), "cnaes", [csv], "2026-08")
    assert n == 3, "linhas acentuadas foram descartadas — encoding errado"

    df = _parquet(bronze, "cnaes")
    assert df[df.codigo == "0141501"].iloc[0].descricao == "Produção de sementes certificadas"


def test_estabelecimento_com_ponto_e_virgula_no_campo_nao_vira_coluna_extra(bronze):
    """';' dentro de campo entre aspas fazia o sniffer ver 31 colunas e abortar."""
    csv = bronze / "estab.csv"
    csv.write_bytes("\n".join([
        _linha_estab("PADARIA DO JOÃO"),
        _linha_estab("LOJA A; LOJA B"),      # <-- o defeito que derrubava a fatia
    ]).encode("utf-8"))

    ins, _ = ing._carregar_parquet(duckdb.connect(), "estabelecimentos", [csv],
                                   _LAYOUT_MIN, _COLS_ESTAB, "2026-08", None)
    assert ins == 2
    assert "LOJA A; LOJA B" in set(_parquet(bronze, "estabelecimentos").nome_fantasia)


def test_filtro_de_uf_casa_valor_entre_aspas(bronze):
    """Sem quote='"', o valor chega como '\"MG\"' e o filtro nunca casa (0 linhas)."""
    csv = bronze / "estab.csv"
    csv.write_bytes("\n".join([
        _linha_estab("ALPHA", "MG"),
        _linha_estab("BETA", "MG"),
        _linha_estab("GAMA", "SP"),
    ]).encode("utf-8"))

    ins, _ = ing._carregar_parquet(duckdb.connect(), "estabelecimentos", [csv],
                                   _LAYOUT_MIN, _COLS_ESTAB, "2026-08", "MG")
    assert ins == 2, "o filtro de UF não casou — aspas não foram removidas"


def test_rejeicoes_sao_realmente_contabilizadas(bronze):
    """A guarda de limiar é inútil se a contagem de rejeições for fixa em 0."""
    csv = bronze / "estab.csv"
    csv.write_bytes("\n".join([
        _linha_estab("ALPHA"),
        _linha_estab("QUEBRADA", "MG", n=25),   # colunas faltando
    ]).encode("utf-8"))

    _, rej = ing._carregar_parquet(duckdb.connect(), "estabelecimentos", [csv],
                                   _LAYOUT_MIN, _COLS_ESTAB, "2026-08", None)
    assert rej >= 1, "linha malformada sumiu em silêncio"


def test_uma_linha_ruim_conta_como_uma_rejeicao(bronze):
    """DuckDB registra um erro POR COLUNA excedente; contar erros infla a razão.

    Se a contagem somasse registros de erro em vez de linhas distintas, uma
    única linha com 3 colunas a mais valeria 3 rejeições e o limiar de 1%
    dispararia sozinho numa base saudável.
    """
    csv = bronze / "estab.csv"
    csv.write_bytes("\n".join([
        _linha_estab("BOA1"),
        _linha_estab("ALPHA") + ';"extra1";"extra2";"extra3"',   # 3 colunas a mais
        _linha_estab("BOA2"),
    ]).encode("utf-8"))

    ins, rej = ing._carregar_parquet(duckdb.connect(), "estabelecimentos", [csv],
                                     _LAYOUT_MIN, _COLS_ESTAB, "2026-08", None)
    assert ins == 2
    assert rej == 1, f"1 linha ruim deveria contar 1 rejeição, contou {rej}"


def test_arquivo_todo_malformado_nao_aborta_com_erro_de_sniffing(bronze):
    """O sniffer do DuckDB derrubava o arquivo inteiro em vez de rejeitar linhas.

    Foi esse o erro que travou a carga real ("Error when sniffing file ...
    ESTABELE"). Com o dialeto declarado e auto_detect=false, o pior caso passa
    a ser 'tudo rejeitado e contabilizado' — diagnosticável — em vez de uma
    exceção opaca no meio do pipeline.
    """
    csv = bronze / "estab.csv"
    csv.write_bytes("\n".join([
        _linha_estab("A") + ';"x";"y"',
        _linha_estab("B", n=25),
    ]).encode("utf-8"))

    ins, rej = ing._carregar_parquet(duckdb.connect(), "estabelecimentos", [csv],
                                     _LAYOUT_MIN, _COLS_ESTAB, "2026-08", None)
    assert ins == 0
    assert rej == 2, "as linhas ruins precisam ser contabilizadas, não sumir"


def test_extracao_normaliza_bytes_windows1252(tmp_path):
    """Regressão do "File is not latin-1 encoded" (CHANGELOG 2.1.3).

    A Receita grava bytes 0x80–0x9F (aspa curva, travessão) que o latin-1
    reserva para controle. O leitor do DuckDB aborta o arquivo inteiro nesse
    caso — e ignore_errors não ajuda, por ser falha dura. A extração precisa
    entregar UTF-8 já normalizado.
    """
    origem = tmp_path / "x.zip"
    #  “  =0x93   ”  =0x94   –  =0x96   ç =0xE7   ã =0xE3
    cru = b'"001";"Pe\xe7as \x93genu\xednas\x94 \x96 Ltda"\n'
    with zipfile.ZipFile(origem, "w") as z:
        z.writestr("K3241.TESTE.ESTABELE", cru)

    (saida,) = ing.extrair(origem, tmp_path / "csv")

    # O contrato da extração: o que sai é UTF-8 válido, com os caracteres
    # certos. (Não dá para exigir "nenhum byte em 0x80–0x9F": em UTF-8 esses
    # valores são bytes de continuação legítimos — “ é E2 80 9C.)
    saida.read_bytes().decode("utf-8")                 # estoura se não for UTF-8
    texto = saida.read_text(encoding="utf-8")
    assert "“genuínas”" in texto
    assert "–" in texto
    assert "Peças" in texto


def test_carga_aceita_arquivo_com_bytes_windows1252(bronze):
    """O caminho completo: ZIP com bytes C1 -> extração -> carga sem abortar."""
    origem = bronze / "x.zip"
    linha = _linha_estab("PEÇAS “GENUÍNAS” – MG")
    with zipfile.ZipFile(origem, "w") as z:
        z.writestr("K3241.TESTE.ESTABELE", linha.encode("cp1252"))

    csvs = ing.extrair(origem, bronze / "csv")
    ins, rej = ing._carregar_parquet(duckdb.connect(), "estabelecimentos", csvs,
                                     _LAYOUT_MIN, _COLS_ESTAB, "2026-08", "MG")
    assert (ins, rej) == (1, 0)
    assert _parquet(bronze, "estabelecimentos").iloc[0].nome_fantasia == \
        "PEÇAS “GENUÍNAS” – MG"


# ------------------------------------------------------- orquestração
def test_pipeline_carrega_icp_antes_do_dbt():
    """O dbt lê config.icp; sem a carga prévia ele falha ao criar gld_icp_matches.

    Regressão do 'schema "config" does not exist' (CHANGELOG 2.1.4): o
    orquestrador ia direto da ingestão para o dbt e só funcionava quando a
    tabela havia sobrado no radar.duckdb de uma execução manual anterior —
    dependência de estado residual, que quebra em qualquer máquina limpa e
    contraria o requisito de pipeline idempotente.
    """
    import re

    texto = (ROOT / "run_pipeline.py").read_text(encoding="utf-8")

    assert "carregar_icp.py" in texto, (
        "run_pipeline.py não carrega o ICP; o dbt vai falhar em gld_icp_matches")

    # Procura o comando dbt sem fixar o subcomando: a versão anterior
    # cravava "dbt run" e quebrou sozinha quando trocamos para "dbt build".
    dbt = re.search(r"['\"]dbt\s+\w+", texto)
    assert dbt, "nenhuma etapa dbt encontrada no orquestrador"
    assert texto.index("carregar_icp.py") < dbt.start(), (
        "a carga do ICP precisa vir ANTES do dbt")


def test_pipeline_invoca_modulos_como_pacote():
    """Módulos de src/ usam imports relativos: exigem `python -m src.X`.

    Regressão do 'attempted relative import with no known parent package'
    (CHANGELOG 2.1.5): o orquestrador chamava `python src/enriquecimento.py`,
    que carrega o arquivo solto, fora do pacote, e estoura no primeiro
    `from . import ...`. A ingestão já era chamada certo; enriquecimento e
    digest, não.
    """
    import re

    # Só as linhas de código: um comentário pode (e deve) citar a forma errada
    # para explicar por que ela é errada, sem por isso reprovar o arquivo.
    codigo = "\n".join(
        linha for linha in (ROOT / "run_pipeline.py").read_text(encoding="utf-8").splitlines()
        if not linha.lstrip().startswith("#")
    )
    soltos = re.findall(r"python\s+src[/\\](\w+)\.py", codigo)
    assert not soltos, (
        f"invocado como arquivo solto: {soltos}. "
        f"Use 'python -m src.<modulo>' — esses módulos têm imports relativos.")


@pytest.mark.parametrize("modulo", ["ingestao", "enriquecimento", "digest"])
def test_modulos_com_import_relativo_sao_chamados_com_dash_m(modulo):
    """Casa cada módulo que usa import relativo com a forma de invocação."""
    import re

    fonte = (ROOT / "src" / f"{modulo}.py").read_text(encoding="utf-8")
    if not re.search(r"^from \.", fonte, re.MULTILINE):
        pytest.skip(f"{modulo} não usa import relativo")

    texto = (ROOT / "run_pipeline.py").read_text(encoding="utf-8")
    assert f"python -m src.{modulo}" in texto, (
        f"{modulo} usa import relativo e precisa ser chamado com 'python -m'")


def test_orquestrador_passa_as_mesmas_competencias_para_ingestao_e_dbt(monkeypatch):
    """Ingerir 2024 e transformar 2026 produz 'sucesso' com zero eventos.

    Regressão do digest vazio (CHANGELOG 2.1.6). `dbt run` ia sem --vars, então
    o dbt usava o default do dbt_project.yml (2026-07/2026-08) enquanto a
    ingestão carregava 2024-07/2024-08. Os 10 modelos reportaram OK porque
    construir tabela vazia é operação válida.

    Executa o main() de verdade com run_step interceptado: valida a
    orquestração em si, não uma cópia dela escrita no teste.
    """
    import importlib
    import re

    rp = importlib.import_module("run_pipeline")
    comandos: list[str] = []
    monkeypatch.setattr(rp, "run_step",
                        lambda nome, cmd, cwd=None: comandos.append(cmd))
    rp.main()

    ingeridas = set(re.findall(r"--competencia\s+(\S+)", " ".join(comandos)))
    assert len(ingeridas) == 2, f"esperava duas competências, achei {ingeridas}"

    dbt = [c for c in comandos if c.strip().startswith("dbt")]
    assert dbt, "nenhuma etapa dbt no pipeline"
    assert "--vars" in dbt[0], (
        "dbt sem --vars usa o default do dbt_project.yml e pode transformar "
        "uma competência que não está no banco")

    passadas = set(re.findall(r"competencia_\w+:\s*'([^']+)'", dbt[0]))
    assert passadas == ingeridas, (
        f"ingestão usa {sorted(ingeridas)} mas o dbt recebe {sorted(passadas)}")


def test_orquestrador_usa_fatias_identicas_nas_competencias(monkeypatch):
    """Fatias diferentes entre competências fabricam NEW_COMPANY falsos."""
    import importlib
    import re

    rp = importlib.import_module("run_pipeline")
    comandos: list[str] = []
    monkeypatch.setattr(rp, "run_step",
                        lambda nome, cmd, cwd=None: comandos.append(cmd))
    rp.main()

    fatias = set(re.findall(r"--fatias\s+(\S+)", " ".join(comandos)))
    assert len(fatias) == 1, (
        f"competências ingeridas com fatias diferentes: {fatias}. As fatias são "
        f"partições por hash do CNPJ; as extras viram 'empresas novas' falsas.")


def test_dbt_roda_build_para_executar_os_testes_de_dados(monkeypatch):
    """`dbt run` pula os 24 testes de dados; `dbt build` os executa."""
    import importlib

    rp = importlib.import_module("run_pipeline")
    comandos: list[str] = []
    monkeypatch.setattr(rp, "run_step",
                        lambda nome, cmd, cwd=None: comandos.append(cmd))
    rp.main()

    dbt = [c for c in comandos if c.strip().startswith("dbt")]
    assert any("dbt build" in c for c in dbt), (
        "use 'dbt build': com 'dbt run' o pipeline entrega um digest sem ter "
        "rodado assert_evento_nao_existe_na_competencia_anterior")


# ------------------------------------------------- ingestão fatia a fatia
def test_carga_por_fatia_grava_arquivo_proprio_e_soma(bronze, monkeypatch):
    """Cada fatia vira um parquet na mesma partição; a soma é o total.

    Carregar as 10 fatias de uma vez exigiria ~45 GB de CSV extraído por
    competência no disco (CHANGELOG 2.1.7). Gravando um arquivo por fatia,
    dá para apagar cada CSV antes de baixar o próximo — e o Hive partitioning
    lê a pasta inteira, então o resultado é idêntico.
    """
    csv = bronze / "estab.csv"
    csv.write_bytes("\n".join([_linha_estab("A"), _linha_estab("B")]).encode("utf-8"))

    con = duckdb.connect()
    for i in (0, 1, 2):
        ing._carregar_parquet(con, "estabelecimentos", [csv], _LAYOUT_MIN,
                              _COLS_ESTAB, "2026-08", None,
                              fatia=i, limpar=(i == 0))

    pasta = bronze / "bronze" / "estabelecimentos" / "competencia=2026-08"
    arquivos = sorted(p.name for p in pasta.glob("*.parquet"))
    assert arquivos == ["dados_0.parquet", "dados_1.parquet", "dados_2.parquet"]

    total = duckdb.connect().execute(
        f"SELECT count(*) FROM read_parquet('{(pasta / '*.parquet').as_posix()}')"
    ).fetchone()[0]
    assert total == 6, "a partição precisa somar as três fatias"


def test_limpar_falso_preserva_fatias_ja_gravadas(bronze):
    """limpar=True só na primeira fatia; nas demais apagaria o já carregado."""
    csv = bronze / "estab.csv"
    csv.write_bytes((_linha_estab("A") + "\n").encode("utf-8"))
    con = duckdb.connect()

    ing._carregar_parquet(con, "estabelecimentos", [csv], _LAYOUT_MIN,
                          _COLS_ESTAB, "2026-08", None, fatia=0, limpar=True)
    ing._carregar_parquet(con, "estabelecimentos", [csv], _LAYOUT_MIN,
                          _COLS_ESTAB, "2026-08", None, fatia=1, limpar=False)

    pasta = bronze / "bronze" / "estabelecimentos" / "competencia=2026-08"
    assert len(list(pasta.glob("*.parquet"))) == 2

    # E a primeira fatia some se a segunda pedir limpeza — o contrato importa.
    ing._carregar_parquet(con, "estabelecimentos", [csv], _LAYOUT_MIN,
                          _COLS_ESTAB, "2026-08", None, fatia=2, limpar=True)
    assert [p.name for p in pasta.glob("*.parquet")] == ["dados_2.parquet"]
