"""Privacidade do piloto — tratamento de empresário individual.

Para empresário individual (natureza jurídica 2135, que inclui o MEI) a razão
social costuma ser o nome da própria pessoa, muitas vezes com o CPF anexado, e
o e-mail/telefone do estabelecimento costuma ser pessoal. No piloto, esses
cadastros saem do digest em seção própria, com:

  * razão social reduzida às iniciais (dígitos, que podem ser CPF, removidos);
  * nome fantasia só quando ele não repete o nome da pessoa;
  * nenhum telefone nem e-mail.

O CNPJ continua aparecendo: é a identificação pública do cadastro, e quem o
consulta na Receita reencontra o nome. A proteção aqui é não distribuir uma
lista de contatos pronta, não tornar o dado inacessível.

Cadastro sem natureza jurídica informada é tratado como protegido: na dúvida,
mascara. Se isso afetar todas as linhas, o digest mostra zero empresas, o que
denuncia o defeito na junção em vez de esconder.

Esta regra vale para o piloto manual com poucos escritórios. Antes de
comercializar, revise-a com quem entende de LGPD.
"""
from __future__ import annotations

import re
import unicodedata

NATUREZAS_INDIVIDUAIS = frozenset({"2135"})   # Empresário (individual)

# Palavras que não viram inicial: ligações e sufixos societários.
_IGNORAR = frozenset({"DA", "DE", "DO", "DAS", "DOS", "E", "ME", "MEI", "EPP", "EIRELI", "LTDA"})

OMITIDO = "(nome omitido)"


def eh_individual(row: dict) -> bool:
    return str(row.get("natureza_juridica") or "").strip() in NATUREZAS_INDIVIDUAIS


def deve_proteger(row: dict) -> bool:
    """Individual, ou natureza desconhecida (na dúvida, protege)."""
    natureza = str(row.get("natureza_juridica") or "").strip()
    return natureza == "" or natureza in NATUREZAS_INDIVIDUAIS


def _tokens(texto: str) -> list[str]:
    ascii_ = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return [t for t in re.split(r"[\s./,;:()\-]+", ascii_.upper()) if t]


def mascarar_nome(nome: str | None) -> str:
    """'JOAO DA SILVA 12345678901' -> 'J. S.'  (dígitos e ligações descartados)."""
    if not nome:
        return OMITIDO
    iniciais = [t[0] + "." for t in _tokens(nome)
                if t not in _IGNORAR and not any(c.isdigit() for c in t)]
    return " ".join(iniciais) or OMITIDO


def _normalizado(texto: str | None) -> str:
    return " ".join(t for t in _tokens(texto or "") if not any(c.isdigit() for c in t))


def para_exibicao(row: dict) -> dict:
    """Cópia da linha com os campos pessoais tratados; não altera a original."""
    saida = dict(row)
    if not deve_proteger(row):
        return saida
    razao = row.get("razao_social")
    fantasia = row.get("nome_fantasia")
    saida["razao_social"] = mascarar_nome(razao)
    saida["nome_fantasia"] = (
        None if not fantasia or _normalizado(fantasia) == _normalizado(razao) else fantasia)
    saida["telefone"] = None
    saida["email"] = None
    return saida
