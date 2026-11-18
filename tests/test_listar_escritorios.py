"""Lista de escritórios: só pessoa jurídica ativa, ordenada por facilidade de contato."""
import importlib.util
from datetime import date
from pathlib import Path

import duckdb

_spec = importlib.util.spec_from_file_location(
    "listar_escritorios", Path(__file__).resolve().parent.parent / "scripts" / "listar_escritorios.py")
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _base():
    con = duckdb.connect(":memory:")
    con.execute("create schema main_silver")
    con.execute("""create table main_silver.slv_estabelecimentos(
        cnpj varchar, cnpj_basico varchar, competencia varchar, nome_fantasia varchar,
        identificador_matriz_filial varchar, situacao_cadastral varchar, cnae_principal varchar,
        id_municipio varchar, uf varchar, bairro varchar, email varchar, telefone varchar,
        data_inicio_atividade date)""")
    con.execute("""create table main_silver.slv_empresas(
        cnpj_basico varchar, competencia varchar, razao_social varchar, natureza_juridica varchar)""")
    con.execute("create table main_silver.slv_dominios(dominio varchar, codigo varchar, descricao varchar)")
    con.execute("insert into main_silver.slv_dominios values ('MUNICIPIO','1','VARGINHA'),('MUNICIPIO','2','CAMPINAS')")
    est = [
        ("1", "1", "Z", "A", "1", "02", "6920601", "1", "MG", "X", "c@contabil-a.com.br", "35999990000", date(2015, 1, 1)),
        ("2", "2", "Z", "B", "1", "02", "6920601", "1", "MG", "X", "b@gmail.com", None, date(2025, 1, 1)),
        ("3", "3", "Z", "C", "1", "02", "6920601", "1", "MG", "X", "mei@gmail.com", "35911110000", date(2020, 1, 1)),
        ("4", "4", "Z", "D", "1", "08", "6920601", "1", "MG", "X", None, None, date(2010, 1, 1)),
        ("5", "5", "Z", "E", "1", "02", "6920601", "2", "SP", "X", None, None, date(2010, 1, 1)),
        ("6", "6", "Z", "F", "1", "02", "5611201", "1", "MG", "X", None, None, date(2010, 1, 1)),
    ]
    con.executemany("insert into main_silver.slv_estabelecimentos values (?,?,?,?,?,?,?,?,?,?,?,?,?)", est)
    emp = [("1", "Z", "ESCRITORIO A LTDA", "2062"), ("2", "Z", "ESCRITORIO B LTDA", "2062"),
           ("3", "Z", "FULANO DE TAL", "2135"), ("4", "Z", "BAIXADA LTDA", "2062"),
           ("5", "Z", "OUTRA CIDADE LTDA", "2062"), ("6", "Z", "RESTAURANTE LTDA", "2062")]
    con.executemany("insert into main_silver.slv_empresas values (?,?,?,?)", emp)
    return con


def test_so_pessoa_juridica_ativa_do_cnae_e_da_regiao():
    r = mod.listar(_base(), ["VARGINHA"], 10, date(2026, 11, 17))
    assert [x["razao_social"] for x in r] == ["ESCRITORIO A LTDA", "ESCRITORIO B LTDA"]


def test_ordem_por_facilidade_de_contato():
    r = mod.listar(_base(), ["VARGINHA"], 10, date(2026, 11, 17))
    assert r[0]["pontos"] == 100 and r[1]["pontos"] == 35
    assert "e-mail de domínio próprio" in r[0]["fatores"]


def test_limite():
    assert len(mod.listar(_base(), ["VARGINHA"], 1, date(2026, 11, 17))) == 1
