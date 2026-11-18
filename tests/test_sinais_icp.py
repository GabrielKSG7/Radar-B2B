"""Seção `sinais` do ICP: carregada do YAML, com defaults seguros."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent


def _carregar(tmp_path, monkeypatch, yaml_texto: str):
    sys.path.insert(0, str(ROOT))
    icp_dir = tmp_path / "icp"
    icp_dir.mkdir()
    (icp_dir / "t.yaml").write_text(yaml_texto, encoding="utf-8")
    mod = importlib.import_module("scripts.carregar_icp")
    monkeypatch.setattr(mod, "ICP_DIR", icp_dir)
    monkeypatch.setattr(mod, "DB_PATH", tmp_path / "t.duckdb")
    mod.carregar()
    return duckdb.connect(str(tmp_path / "t.duckdb"), read_only=True)


BASE = "nome: t\noferta: x\ncnae:\n  primarios: ['5611201']\n"


def test_sinais_do_yaml_viram_colunas_e_tabelas(tmp_path, monkeypatch):
    con = _carregar(tmp_path, monkeypatch, BASE + (
        "geografia:\n  municipios: [VARGINHA]\n  ddd: [35, 31]\n"
        "sinais:\n"
        "  contador_atual:\n    penalidade: 30\n    limiar_compartilhamento: 7\n"
        "    limiar_dominio_intermediario: 40\n"
        "    padroes_dominio: [Contab, contador]\n"
        "  fora_da_regiao:\n    penalidade: 10\n"
        "  grupo_empresarial:\n    penalidade: 20\n"))
    assert con.execute(
        "SELECT pen_contador_atual, pen_fora_da_regiao, pen_grupo_empresarial,"
        " limiar_compartilhamento, limiar_dominio_intermediario FROM config.icp"
    ).fetchall() == [(30, 10, 20, 7, 40)]
    assert con.execute("SELECT ddd FROM config.icp_ddd ORDER BY ddd").fetchall() \
        == [("31",), ("35",)]
    # padrões em minúsculas: o SQL compara com o domínio já em minúsculas
    assert con.execute("SELECT padrao FROM config.icp_padrao_contador ORDER BY 1"
                       ).fetchall() == [("contab",), ("contador",)]


def test_icp_sem_secao_sinais_nao_rebaixa_nada(tmp_path, monkeypatch):
    """ICP antigo (sem `sinais`) continua válido: penalidades 0, sem DDD."""
    con = _carregar(tmp_path, monkeypatch, BASE)
    assert con.execute(
        "SELECT pen_contador_atual, pen_fora_da_regiao, pen_grupo_empresarial,"
        " limiar_dominio_intermediario FROM config.icp").fetchall() == [(0, 0, 0, 0)]
    assert con.execute("SELECT count(*) FROM config.icp_ddd").fetchall() == [(0,)]
    assert con.execute("SELECT count(*) FROM config.icp_padrao_contador"
                       ).fetchall() == [(0,)]


def test_yaml_do_projeto_declara_os_tres_sinais():
    """O ICP real deve ter os sinais ligados — penalidade 0 os desligaria."""
    import yaml

    icp = yaml.safe_load((ROOT / "config" / "icp" / "contabilidade_sul_mg.yaml"
                          ).read_text(encoding="utf-8"))
    sinais = icp["sinais"]
    assert sinais["contador_atual"]["penalidade"] > 0
    assert sinais["fora_da_regiao"]["penalidade"] > 0
    assert sinais["grupo_empresarial"]["penalidade"] > 0
    # sem este limiar, domínio de plataforma com centenas de empresas viraria
    # "grupo" (-20) em vez de contador (-30)
    assert sinais["contador_atual"]["limiar_dominio_intermediario"] > \
        sinais["contador_atual"]["limiar_compartilhamento"]
    assert icp["geografia"]["ddd"], "sem DDD o sinal fora_da_regiao fica desligado"
    # sem penalidade o sinal não pode empurrar o score para fora de [0, 100]
    pesos = sum(icp["pesos"].values())
    assert pesos == 100


def test_auditar_sinais_conta_e_lista_so_dominios(tmp_path):
    from scripts import auditar_sinais

    con = duckdb.connect(str(tmp_path / "a.duckdb"))
    con.execute("CREATE SCHEMA main_gold")
    con.execute("""CREATE TABLE main_gold.gld_icp_matches AS SELECT * FROM (VALUES
        (true,  true,  false, false, 1,  'escritoriocontab.com.br', false, false, false, true,  true),
        (true,  false, true,  false, 9,  'bpofiscal.com.br',        false, true,  false, true,  false),
        (false, false, false, true,  6,  'lojas.com.br',            false, false, true,  false, true)
    ) t(sinal_contador_atual, contador_por_padrao, contador_por_compartilhamento,
        grupo_por_dominio, n_empresas_contato, email_dominio, sinal_fora_da_regiao,
        sinal_filial, sinal_spe, email_valido, telefone_valido)""")
    con.execute("CREATE TABLE main_gold.gld_opportunities AS "
                "SELECT 1 AS event_id UNION ALL SELECT 2")

    r = auditar_sinais.auditar(con)
    assert (r["candidatos"], r["no_digest"], r["fora_por_desconto"]) == (3, 2, 1)
    assert r["contador_atual"] == 2 and r["contador_por_padrao"] == 1
    assert r["filial"] == 1 and r["spe"] == 1 and r["sem_email_valido"] == 1
    assert r["dominios_compartilhados"] == [("bpofiscal.com.br", 9, 1)]
    assert r["grupo_por_dominio"] == 1
    assert r["dominios_de_grupo"] == [("lojas.com.br", 6, 1)]
