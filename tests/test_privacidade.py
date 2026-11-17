"""Privacidade do piloto: empresário individual sai mascarado e sem contato.

Os testes de digest chamam a função de produção (`digest.gerar`) sobre um
DuckDB temporário, e não uma cópia da lógica.
"""
from __future__ import annotations

import duckdb

from src import digest, enriquecimento, privacidade


def test_mascarar_nome_remove_cpf_e_ligacoes():
    assert privacidade.mascarar_nome("JOAO DA SILVA 12345678901") == "J. S."
    assert privacidade.mascarar_nome("MARIA APARECIDA DOS SANTOS ME") == "M. A. S."


def test_mascarar_nome_vazio_ou_so_digitos():
    assert privacidade.mascarar_nome(None) == privacidade.OMITIDO
    assert privacidade.mascarar_nome("12345678901") == privacidade.OMITIDO


def test_deve_proteger():
    assert privacidade.deve_proteger({"natureza_juridica": "2135"})
    assert privacidade.deve_proteger({"natureza_juridica": None})     # na dúvida, protege
    assert not privacidade.deve_proteger({"natureza_juridica": "2062"})


def test_fantasia_que_repete_o_nome_da_pessoa_e_omitida():
    row = {"natureza_juridica": "2135", "razao_social": "JOAO DA SILVA 12345678901",
           "nome_fantasia": "Joao da Silva", "telefone": "3599999999", "email": "j@x.com"}
    x = privacidade.para_exibicao(row)
    assert x["nome_fantasia"] is None and x["telefone"] is None and x["email"] is None
    assert row["telefone"] == "3599999999"          # a original não é alterada


def test_fantasia_comercial_e_mantida():
    row = {"natureza_juridica": "2135", "razao_social": "JOAO DA SILVA 12345678901",
           "nome_fantasia": "Padaria Pao Quente"}
    assert privacidade.para_exibicao(row)["nome_fantasia"] == "Padaria Pao Quente"


def test_empresa_nao_e_alterada():
    row = {"natureza_juridica": "2062", "razao_social": "ACME TECNOLOGIA LTDA",
           "telefone": "3511111111", "email": "contato@acme.com"}
    assert privacidade.para_exibicao(row) == row


def test_prompt_do_llm_nao_leva_nome_de_individual():
    row = {"event_type": "NEW_COMPANY", "event_date": "2026-10-20",
           "detection_confidence": "alta", "natureza_juridica": "2135",
           "razao_social": "MARIA APARECIDA SOUZA 11122233344",
           "nome_fantasia": None, "cnae_descricao": "Cabeleireiros",
           "municipio_nome": "VARGINHA", "uf": "MG", "porte_descricao": "Micro Empresa",
           "capital_social": 1000, "icp_nome": "x", "icp_oferta": "y",
           "opportunity_score": 60, "score_fatores": "{}"}
    p = enriquecimento._prompt(row)
    assert "MARIA" not in p and "SOUZA" not in p and "11122233344" not in p
    assert "M. A. S." in p


def _banco(tmp_path):
    caminho = tmp_path / "radar.duckdb"
    con = duckdb.connect(str(caminho))
    con.execute("CREATE SCHEMA main_gold; CREATE SCHEMA gold;")
    con.execute("""
        CREATE TABLE main_gold.gld_opportunities (
            event_id VARCHAR, cnpj VARCHAR, event_type VARCHAR, event_date DATE,
            source_competence VARCHAR, icp_nome VARCHAR, razao_social VARCHAR,
            nome_fantasia VARCHAR, municipio_nome VARCHAR, uf VARCHAR,
            cnae_principal VARCHAR, cnae_descricao VARCHAR, porte_descricao VARCHAR,
            natureza_juridica VARCHAR, capital_social DOUBLE, email VARCHAR,
            telefone VARCHAR, dias_desde_evento INTEGER, opportunity_score INTEGER,
            score_fatores VARCHAR, score_explanation VARCHAR)""")
    con.execute("""
        CREATE TABLE gold.gld_opportunities_enriched (
            event_id VARCHAR, atividade_provavel VARCHAR, publico_alvo VARCHAR,
            hipotese_de_dor VARCHAR, explicacao_relevancia VARCHAR,
            acao_sugerida VARCHAR, confianca VARCHAR, origem VARCHAR, modelo VARCHAR)""")
    con.execute("""INSERT INTO main_gold.gld_opportunities VALUES
        ('e1','11111111000191','NEW_COMPANY','2026-10-20','2026-11','icp',
         'ACME TECNOLOGIA LTDA','Acme','VARGINHA','MG','6201501','Software','Demais',
         '2062',50000,'contato@acme.com','3511111111',10,80,'{}','Aberta há 10 dias'),
        ('e2','22222222000192','NEW_COMPANY','2026-10-22','2026-11','icp',
         'MARIA APARECIDA SOUZA 11122233344','Studio Bela','VARGINHA','MG','9602501',
         'Cabeleireiros','Micro Empresa','2135',1000,'maria@gmail.com','35988887777',
         8,70,'{}','Aberta há 8 dias')""")
    con.close()
    return caminho


def test_digest_nao_vaza_dados_de_individuo(tmp_path, monkeypatch):
    monkeypatch.setattr(digest, "DB_PATH", _banco(tmp_path))
    monkeypatch.setattr(digest, "DIGEST_DIR", tmp_path / "digests")

    caminho_md, n = digest.gerar()
    assert n == 2

    md = open(caminho_md, encoding="utf-8").read()
    csv_txt = next((tmp_path / "digests").glob("radar_piloto_*.csv")).read_text(
        encoding="utf-8-sig")

    for texto in (md, csv_txt):
        for segredo in ("MARIA", "SOUZA", "11122233344", "maria@gmail.com", "35988887777"):
            assert segredo not in texto, f"{segredo!r} vazou"
        assert "ACME TECNOLOGIA LTDA" in texto           # empresa segue completa
        assert "contato@acme.com" in texto
        assert "22222222000192" in texto                 # CNPJ público permanece
    assert "## Empresários individuais (1)" in md
    assert "Studio Bela" in md                           # fantasia comercial é mantida
    assert "M. A. S." in csv_txt
