{#
  Âncora temporal do pipeline.

  Todo cálculo de "quão recente é isto" tem de se referir à COMPETÊNCIA
  processada, nunca à data em que o pipeline foi executado.

  Por quê: a Receita publica snapshots mensais. Processar a competência
  2024-08 hoje, em 2026, e medir recência contra `current_date` dá ~770 dias
  para uma empresa aberta em agosto de 2024 — que é o mês correto. Com a
  janela de 45 dias do ICP, isso descartava 100% dos eventos e o Gold saía
  vazio, sem erro nenhum.

  Além de corrigir o resultado, isto torna o pipeline IDEMPOTENTE: reprocessar
  a mesma competência amanhã, ou daqui a um ano, produz exatamente o mesmo
  score. É a mesma razão pela qual `evt_new_company` abandonou o filtro por
  `current_date - 30` da V3.

  Convenção: a referência é o ÚLTIMO dia da competência — o instante em que a
  fotografia daquele mês está completa.

  ATENÇÃO (2.5.0): para RECÊNCIA, esta macro virou só o TETO e o fallback. A
  Receita publica o snapshot no meio do mês (o de 2026-11 termina em 15/11),
  então a idade dos eventos é medida contra o último dia com volume real de
  eventos — ver o CTE `ancora` em gld_icp_matches.sql.
#}
{% macro data_referencia(competencia=none) %}
    (
        strptime('{{ competencia or var("competencia_atual") }}-01', '%Y-%m-%d')::date
        + interval 1 month - interval 1 day
    )::date
{% endmacro %}
