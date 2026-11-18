-- O score precisa ser exatamente a soma dos fatores persistidos (seis fatores
-- positivos e três ajustes negativos), limitada a [0, 100]. Garante que a
-- explicação mostrada ao usuário não diverge do número.
select
    event_id,
    opportunity_score,
    (   json_extract(score_fatores, '$.recencia')::int
      + json_extract(score_fatores, '$.cnae')::int
      + json_extract(score_fatores, '$.porte')::int
      + json_extract(score_fatores, '$.localizacao')::int
      + json_extract(score_fatores, '$.capital')::int
      + json_extract(score_fatores, '$.contexto')::int
      + json_extract(score_fatores, '$.ajuste_contador')::int
      + json_extract(score_fatores, '$.ajuste_regiao')::int
      + json_extract(score_fatores, '$.ajuste_grupo')::int
    ) as soma_fatores
from {{ ref('gld_opportunities') }}
where opportunity_score <> least(100, greatest(0, soma_fatores))
