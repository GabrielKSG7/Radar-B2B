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


def test_digest_informa_ate_quando_vao_os_dados_da_receita(tmp_path, monkeypatch):
    """A idade dos eventos é medida contra a âncora; o leitor precisa vê-la."""
    caminho = _banco(tmp_path)
    con = duckdb.connect(str(caminho))
    con.execute("ALTER TABLE main_gold.gld_opportunities "
                "ADD COLUMN data_ancora_recencia DATE")
    con.execute("UPDATE main_gold.gld_opportunities "
                "SET data_ancora_recencia = DATE '2026-11-15'")
    con.close()
    monkeypatch.setattr(digest, "DB_PATH", caminho)
    monkeypatch.setattr(digest, "DIGEST_DIR", tmp_path / "digests")

    md = open(digest.gerar()[0], encoding="utf-8").read()
    assert "**Dados da Receita até:** 2026-11-15" in md


def test_digest_sem_ancora_nao_quebra(tmp_path, monkeypatch):
    """Banco gerado antes da 2.5.0 não tem a coluna: o cabeçalho só omite a linha."""
    monkeypatch.setattr(digest, "DB_PATH", _banco(tmp_path))
    monkeypatch.setattr(digest, "DIGEST_DIR", tmp_path / "digests")
    md = open(digest.gerar()[0], encoding="utf-8").read()
    assert "Dados da Receita até" not in md


# ------------------------------------------------------------------- v2.5.1
def _insere(caminho, **campos):
    """Segunda empresa (LTDA) com e-mail pessoal, fatores e explicação de contato."""
    base = {
        "event_id": "e3", "cnpj": "33333333000193", "razao_social": "BETA COMERCIO LTDA",
        "nome_fantasia": "Beta", "natureza_juridica": "2062", "capital_social": 20000,
        "email": "guilherme.beta@gmail.com", "telefone": "3533334444",
        "opportunity_score": 75,
        "score_fatores": '{"recencia": 27, "cnae": 25, "ajuste_regiao": -10}',
        "score_explanation": "Aberta há 4 dias (+27) · Contato disponível (+10)",
    }
    base.update(campos)
    con = duckdb.connect(str(caminho))
    con.execute(
        "INSERT INTO main_gold.gld_opportunities VALUES (?,?,'NEW_COMPANY',"
        "'2026-11-11','2026-11','icp',?,?,'VARGINHA','MG','5611201','Restaurantes',"
        "'Micro Empresa',?,?,?,?,4,?,?,?)",
        [base["event_id"], base["cnpj"], base["razao_social"], base["nome_fantasia"],
         base["natureza_juridica"], base["capital_social"], base["email"],
         base["telefone"], base["opportunity_score"], base["score_fatores"],
         base["score_explanation"]])
    con.close()


def _gerar(tmp_path, monkeypatch, **campos):
    caminho = _banco(tmp_path)
    _insere(caminho, **campos)
    monkeypatch.setattr(digest, "DB_PATH", caminho)
    monkeypatch.setattr(digest, "DIGEST_DIR", tmp_path / "digests")
    md = open(digest.gerar()[0], encoding="utf-8").read()
    csv_txt = next((tmp_path / "digests").glob("radar_piloto_*.csv")).read_text(
        encoding="utf-8-sig")
    return md, csv_txt


def test_email_pessoal_de_empresa_e_omitido_mas_telefone_fica(tmp_path, monkeypatch):
    md, csv_txt = _gerar(tmp_path, monkeypatch)
    for texto in (md, csv_txt):
        assert "guilherme.beta@gmail.com" not in texto
        assert "e-mail pessoal omitido" in texto
        assert "3533334444" in texto                       # telefone segue
    assert "contato@acme.com" in md                        # e-mail corporativo segue


def test_email_pessoal_volta_com_a_chave_desligada(tmp_path, monkeypatch):
    monkeypatch.setenv("RADAR_OCULTAR_EMAIL_PESSOAL", "0")
    md, _ = _gerar(tmp_path, monkeypatch)
    assert "guilherme.beta@gmail.com" in md


def test_fatores_negativos_nao_saem_como_mais_menos(tmp_path, monkeypatch):
    md, _ = _gerar(tmp_path, monkeypatch)
    assert "ajuste_regiao -10" in md and "recencia +27" in md
    assert "+-" not in md


def test_csv_usa_virgula_decimal_no_capital(tmp_path, monkeypatch):
    _, csv_txt = _gerar(tmp_path, monkeypatch)
    linhas = [linha.split(";") for linha in csv_txt.splitlines()]
    cab = linhas[0]
    beta = next(linha for linha in linhas if "BETA COMERCIO LTDA" in linha)
    assert beta[cab.index("capital_social")] == "20000,00"
    assert "20000.0" not in csv_txt


def test_individuo_nao_diz_contato_disponivel_com_contato_omitido(
        tmp_path, monkeypatch):
    caminho = _banco(tmp_path)
    con = duckdb.connect(str(caminho))
    con.execute("UPDATE main_gold.gld_opportunities SET score_explanation = "
                "'Aberta há 8 dias (+25) · Contato disponível (+10)' "
                "WHERE event_id = 'e2'")
    con.close()
    monkeypatch.setattr(digest, "DB_PATH", caminho)
    monkeypatch.setattr(digest, "DIGEST_DIR", tmp_path / "digests")
    digest.gerar()
    csv_txt = next((tmp_path / "digests").glob("radar_piloto_*.csv")).read_text(
        encoding="utf-8-sig")
    linha = next(linha for linha in csv_txt.splitlines() if "22222222000192" in linha)
    assert "Contato disponível" not in linha
    assert "contato omitido no piloto" in linha


def test_lista_de_provedores_pessoais_bate_com_a_do_dbt():
    """Python (digest) e SQL (sinal de compartilhamento) usam a mesma lista."""
    import re
    from pathlib import Path

    sql = (Path(__file__).resolve().parent.parent / "dbt_radar" / "models" / "gold"
           / "gld_icp_matches.sql").read_text(encoding="utf-8")
    bloco = sql[sql.index("provedores_livres as ("):]
    bloco = bloco[:bloco.index("]) as dominio")]
    assert set(re.findall(r"'([a-z0-9.\-]+)'", bloco)) == privacidade.PROVEDORES_PESSOAIS


# ------------------------------------------------------------------- v2.5.2
def test_email_de_provavel_contador_nao_e_mostrado(tmp_path, monkeypatch):
    """O e-mail de uma empresa com sinal de contador atual é o do escritório
    concorrente: nem no digest nem no CSV."""
    caminho = _banco(tmp_path)
    con = duckdb.connect(str(caminho))
    con.execute("ALTER TABLE main_gold.gld_opportunities "
                "ADD COLUMN sinal_contador_atual BOOLEAN")
    con.execute("UPDATE main_gold.gld_opportunities SET sinal_contador_atual = false")
    con.execute("UPDATE main_gold.gld_opportunities SET "
                "email = 'cadastro@escritoriosigilo.com.br', "
                "sinal_contador_atual = true WHERE event_id = 'e1'")
    con.close()
    monkeypatch.setattr(digest, "DB_PATH", caminho)
    monkeypatch.setattr(digest, "DIGEST_DIR", tmp_path / "digests")
    md = open(digest.gerar()[0], encoding="utf-8").read()
    csv_txt = next((tmp_path / "digests").glob("radar_piloto_*.csv")).read_text(
        encoding="utf-8-sig")
    for texto in (md, csv_txt):
        assert "escritoriosigilo" not in texto
        assert "e-mail de provável contador omitido" in texto
        assert "3511111111" in texto                    # o telefone da empresa fica


def test_empresa_sem_sinal_de_contador_mantem_email_corporativo():
    row = {"natureza_juridica": "2062", "email": "contato@acme.com",
           "sinal_contador_atual": False}
    assert privacidade.para_exibicao(row)["email"] == "contato@acme.com"


# ---- v2.5.3: curadoria manual e telefone de mentira -------------------------

def test_telefone_valido():
    assert privacidade.telefone_valido("3532215704")
    assert privacidade.telefone_valido("35999990000")
    assert not privacidade.telefone_valido("4444444444")
    assert not privacidade.telefone_valido("123")
    assert not privacidade.telefone_valido(None)


def test_telefone_de_mentira_nao_aparece_no_digest(tmp_path, monkeypatch):
    md, csv_txt = _gerar(tmp_path, monkeypatch, telefone="4444444444")
    assert "4444444444" not in md and "4444444444" not in csv_txt


def test_carregar_exclusoes_ignora_comentario_e_aceita_pontuacao(tmp_path):
    arq = tmp_path / "excluir.txt"
    arq.write_text("# comentário\n\n33.333.333/0001-93;fora do perfil\nlixo;x\n",
                   encoding="utf-8")
    assert digest.carregar_exclusoes(arq) == {"33333333000193": "fora do perfil"}
    assert digest.carregar_exclusoes(tmp_path / "nao_existe.txt") == {}


def test_cnpj_excluido_some_do_digest_e_da_contagem(tmp_path, monkeypatch):
    arq = tmp_path / "excluir.txt"
    arq.write_text("33333333000193;fora do perfil\n", encoding="utf-8")
    monkeypatch.setattr(digest, "ARQUIVO_EXCLUSOES", arq)
    monkeypatch.setattr(digest, "carregar_exclusoes",
                        lambda caminho=arq: {"33333333000193": "x"})
    md, csv_txt = _gerar(tmp_path, monkeypatch)
    assert "BETA COMERCIO" not in md and "BETA COMERCIO" not in csv_txt


def test_cabecalho_do_digest_nao_usa_a_sigla_icp(tmp_path, monkeypatch):
    md, _ = _gerar(tmp_path, monkeypatch)
    assert "(ICP)" not in md and "Perfil de busca" in md


def test_contato_disponivel_some_quando_nada_de_contato_e_exibido():
    row = {"natureza_juridica": "2062", "telefone": "4444444444",
           "email": "x@escritorio.com.br", "sinal_contador_atual": True,
           "score_explanation": "Contato disponível (+6) · outro (+5)"}
    assert "contato omitido no piloto" in privacidade.explicacao_para_exibicao(row)


def test_contato_disponivel_fica_quando_o_telefone_e_exibido():
    row = {"natureza_juridica": "2062", "telefone": "3532215704",
           "email": "x@escritorio.com.br", "sinal_contador_atual": True,
           "score_explanation": "Contato disponível (+10)"}
    assert privacidade.explicacao_para_exibicao(row) == "Contato disponível (+10)"
