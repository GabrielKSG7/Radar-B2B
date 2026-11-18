"""Retry, modelo reserva, mensagens de erro e regras do texto da IA (v2.5.1)."""
from __future__ import annotations

import json

import pytest

from src import enriquecimento as enr


class ErroHttp(Exception):
    """Imita google.genai.errors.APIError: carrega o código HTTP em .code."""

    def __init__(self, code: int):
        super().__init__(f"{code} erro simulado")
        self.code = code


def _resp(**campos):
    base = {
        "atividade_provavel": "Restaurante.",
        "publico_alvo": "Consumidores locais.",
        "hipotese_de_dor": "Empresas deste setor costumam ter fluxo de caixa curto.",
        "explicacao_relevancia": "Empresa recém-aberta; pode se beneficiar de BPO.",
        "acao_sugerida": "Contato consultivo apresentando a oferta do ICP.",
        "confianca": "media",
    }
    base.update(campos)

    class R:
        text = json.dumps(base)
    return R()


class ClienteFalso:
    """Devolve, na ordem, respostas ou exceções; guarda os modelos chamados."""

    def __init__(self, roteiro):
        self.roteiro = list(roteiro)
        self.modelos = []
        self.prompts = []
        self.models = self

    def generate_content(self, model, contents, config):
        self.modelos.append(model)
        self.prompts.append(contents)
        item = self.roteiro.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


ROW = {
    "event_id": "e1", "event_type": "NEW_COMPANY", "event_date": "2026-11-10",
    "detection_confidence": "alta", "razao_social": "ALFA LTDA",
    "nome_fantasia": "ALFA", "cnae_descricao": "Restaurantes e similares",
    "municipio_nome": "VARGINHA", "uf": "MG", "porte_descricao": "Micro Empresa",
    "capital_social": 1000, "icp_nome": "t", "icp_oferta": "contabilidade",
    "opportunity_score": 80, "score_fatores": "{}",
    "natureza_juridica": "2062",
}


@pytest.fixture(autouse=True)
def _modelos(monkeypatch):
    monkeypatch.setattr(enr, "MODELO", "principal")
    monkeypatch.setattr(enr, "MODELO_RESERVA", None)


def test_503_espera_e_tenta_de_novo_no_mesmo_modelo():
    cli = ClienteFalso([ErroHttp(503), ErroHttp(503), _resp()])
    esperas = []
    ctx, modelo = enr.chamar_com_resiliencia(cli, ROW, dormir=esperas.append)
    assert modelo == "principal" and cli.modelos == ["principal"] * 3
    assert esperas == [5, 15]            # backoff crescente
    assert ctx["confianca"] == "media"


def test_esgotado_o_principal_usa_o_modelo_reserva(monkeypatch):
    monkeypatch.setattr(enr, "MODELO_RESERVA", "reserva")
    cli = ClienteFalso([ErroHttp(503)] * 4 + [_resp()])   # 1 + 3 esperas
    ctx, modelo = enr.chamar_com_resiliencia(cli, ROW, dormir=lambda s: None)
    assert modelo == "reserva"
    assert cli.modelos == ["principal"] * 4 + ["reserva"]


def test_sem_reserva_o_503_persistente_vira_erro_transitorio():
    cli = ClienteFalso([ErroHttp(429)] * 4)
    with pytest.raises(enr.ErroTransitorioLLM):
        enr.chamar_com_resiliencia(cli, ROW, dormir=lambda s: None)


@pytest.mark.parametrize("codigo", [401, 403, 404])
def test_config_errada_para_na_hora_com_mensagem_da_chave(codigo):
    cli = ClienteFalso([ErroHttp(codigo), _resp()])
    esperas = []
    with pytest.raises(enr.ErroConfiguracaoLLM, match="verifique a chave"):
        enr.chamar_com_resiliencia(cli, ROW, dormir=esperas.append)
    assert esperas == [] and len(cli.modelos) == 1     # sem retry, sem reserva


def test_503_nao_diz_para_verificar_a_chave():
    cli = ClienteFalso([ErroHttp(503)] * 4)
    with pytest.raises(enr.ErroTransitorioLLM) as ei:
        enr.chamar_com_resiliencia(cli, ROW, dormir=lambda s: None)
    assert "chave" not in str(ei.value).lower()


def test_texto_proibido_pede_segunda_resposta():
    ruim = _resp(explicacao_relevancia="A empresa necessita urgentemente de apoio.")
    cli = ClienteFalso([ruim, _resp()])
    ctx, _ = enr.chamar_com_resiliencia(cli, ROW, dormir=lambda s: None)
    assert len(cli.modelos) == 2
    assert "VIOLOU AS REGRAS" in cli.prompts[1]
    assert "urgente" not in ctx["explicacao_relevancia"].lower()


def test_texto_proibido_duas_vezes_e_rejeitado():
    ruim = _resp(acao_sugerida="Chamar no LinkedIn e oferecer pacote de abertura.")
    cli = ClienteFalso([ruim, ruim])
    with pytest.raises(enr.ConteudoRejeitado):
        enr.chamar_com_resiliencia(cli, ROW, dormir=lambda s: None)


@pytest.mark.parametrize("campo,texto", [
    ("acao_sugerida", "Oferecer um pacote de abertura de empresa."),
    ("hipotese_de_dor", "Obrigações complexas do Simples Nacional."),
    ("explicacao_relevancia", "Ela precisa de contabilidade."),
    ("acao_sugerida", "Visita presencial ao restaurante."),
    ("explicacao_relevancia", "Ajuda para estructurar a operação."),
])
def test_violacoes_detecta_os_vicios_do_digest_real(campo, texto):
    assert enr.violacoes({campo: texto}), texto


@pytest.mark.parametrize("campo,texto", [
    # "necessitam" descreve os CLIENTES da empresa, não a empresa do lead
    ("publico_alvo", "Empresas que necessitam de transporte de cargas."),
    ("publico_alvo", "Consumidores locais e visitantes do município."),
    ("hipotese_de_dor", "Costumam enfrentar desafios com fluxo de caixa."),
    ("acao_sugerida", "Contato consultivo na fase inicial da operação."),
    ("atividade_provavel", "Comércio varejista, operando como Rei das Meias."),
])
def test_violacoes_nao_pega_uso_legitimo(campo, texto):
    assert enr.violacoes({campo: texto}) == [], texto


def test_sinal_de_contador_ou_grupo_limita_confianca_a_baixa():
    ctx = enr.calibrar_confianca({"confianca": "alta"},
                                 {**ROW, "sinal_contador_atual": True})
    assert ctx["confianca"] == "baixa"
    ctx = enr.calibrar_confianca({"confianca": "media"},
                                 {**ROW, "sinal_grupo_empresarial": True})
    assert ctx["confianca"] == "baixa"


def test_ddd_fora_da_regiao_limita_a_media_e_sem_sinal_nao_mexe():
    assert enr.calibrar_confianca(
        {"confianca": "alta"}, {**ROW, "sinal_fora_da_regiao": True}
    )["confianca"] == "media"
    assert enr.calibrar_confianca({"confianca": "alta"}, ROW)["confianca"] == "alta"
    assert enr.calibrar_confianca(
        {"confianca": "baixa"}, {**ROW, "sinal_fora_da_regiao": True}
    )["confianca"] == "baixa"          # nunca sobe


def test_prompt_informa_os_sinais_sem_vazar_contato():
    p = enr._prompt({**ROW, "sinal_contador_atual": True, "email": "joao@gmail.com",
                     "telefone": "35999990000"})
    assert "Indício de que já tem contador: sim" in p
    assert "joao@gmail.com" not in p and "35999990000" not in p


def test_system_proibe_abertura_urgencia_e_regime():
    for trecho in ("JÁ ESTÁ ABERTA", "urgência", "Simples Nacional", "canais de contato",
                   '"media" é o padrão'):
        assert trecho in enr.SYSTEM, trecho


# ----------------------------------------------- enriquecer() de ponta a ponta
def _banco(tmp_path, monkeypatch, n: int):
    import duckdb

    db = tmp_path / "r.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE SCHEMA main_gold")
    colunas = {k: v for k, v in ROW.items() if k != "event_id"}
    con.execute("CREATE TABLE main_gold.gld_opportunities (event_id VARCHAR, "
                + ", ".join(f"{c} VARCHAR" for c in colunas) + ", "
                "dias_desde_evento INTEGER)")
    for i in range(n):
        con.execute(
            "INSERT INTO main_gold.gld_opportunities VALUES (?, "
            + ",".join("?" * len(colunas)) + ", 5)",
            [f"ev{i:03d}" + "x" * 10] + [str(v) for v in colunas.values()])
    con.close()
    monkeypatch.setattr(enr, "DB_PATH", db)
    monkeypatch.setattr(enr, "API_KEY", "chave-falsa")
    monkeypatch.setattr(enr.time, "sleep", lambda s: None)
    return db


def _linhas(db):
    import duckdb

    con = duckdb.connect(str(db), read_only=True)
    r = con.execute(f"SELECT origem, count(*) FROM {enr.TABELA} GROUP BY 1").fetchall()
    con.close()
    return dict(r)


def test_enriquecer_503_em_massa_para_com_mensagem_certa_e_guarda_o_que_fez(
        tmp_path, monkeypatch):
    db = _banco(tmp_path, monkeypatch, 8)
    cli = ClienteFalso([ErroHttp(503)] * 200)
    monkeypatch.setattr(enr, "_cliente", lambda: cli)
    with pytest.raises(RuntimeError) as ei:
        enr.enriquecer(usar_ia=True)
    msg = str(ei.value)
    assert "sobrecarregado" in msg and "RADAR_LLM_FALLBACK" in msg
    assert "chave" not in msg.lower()
    assert sum(_linhas(db).values()) >= 5        # não perdeu o que já processou


def test_enriquecer_401_para_com_aviso_da_chave(tmp_path, monkeypatch):
    _banco(tmp_path, monkeypatch, 3)
    cli = ClienteFalso([ErroHttp(401)] * 10)
    monkeypatch.setattr(enr, "_cliente", lambda: cli)
    with pytest.raises(enr.ErroConfiguracaoLLM, match="verifique a chave"):
        enr.enriquecer(usar_ia=True)
    assert len(cli.modelos) == 1                 # não insistiu


def test_enriquecer_registra_o_modelo_reserva_usado(tmp_path, monkeypatch):
    db = _banco(tmp_path, monkeypatch, 1)
    monkeypatch.setattr(enr, "MODELO_RESERVA", "reserva")
    cli = ClienteFalso([ErroHttp(503)] * 4 + [_resp()])
    monkeypatch.setattr(enr, "_cliente", lambda: cli)
    r = enr.enriquecer(usar_ia=True)
    assert r["llm"] == 1 and r["falhas"] == 0
    import duckdb
    con = duckdb.connect(str(db), read_only=True)
    assert con.execute(f"SELECT modelo, origem FROM {enr.TABELA}").fetchall() \
        == [("reserva", "llm")]
    con.close()


# ------------------------------------------------------------------- v2.5.2
def test_texto_desacentuado_e_detectado():
    achados = enr.violacoes({
        "atividade_provavel": "Operacao de restaurante e servicos de alimentacao",
        "acao_sugerida": "Contato consultivo."})
    assert achados and "sem acentuação" in achados[0]
    assert "operacao" in achados[0] and "servicos" in achados[0]


def test_texto_acentuado_passa():
    assert enr.violacoes({
        "atividade_provavel": "Operação de restaurante e serviços de alimentação",
        "hipotese_de_dor": "Costumam ter dificuldade na conciliação de pagamentos.",
        "publico_alvo": "Consumidores finais em Três Corações."}) == []


def test_razao_social_em_maiusculas_nao_conta_como_desacentuada():
    assert enr.violacoes({"atividade_provavel":
                          "Atua como COMERCIO RDM VARGINHA LTDA no varejo."}) == []


def test_texto_desacentuado_pede_reescrita_e_aceita_a_corrigida():
    ruim = _resp(atividade_provavel="Operacao de restaurante e servicos de alimentacao")
    cli = ClienteFalso([ruim, _resp()])
    ctx, _ = enr.chamar_com_resiliencia(cli, ROW, dormir=lambda s: None)
    assert len(cli.modelos) == 2 and "sem acentuação" in cli.prompts[1]
    assert ctx["atividade_provavel"] == "Restaurante."


def test_system_exige_acentuacao():
    assert "acentuação" in enr.SYSTEM
