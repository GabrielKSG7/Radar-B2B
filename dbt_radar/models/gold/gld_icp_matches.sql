{{ config(materialized='table') }}

/*
  ICP MATCHING — dirigido por configuração (config.icp*), não por regra
  hardcoded no SQL.

  Correções em relação à V3:
    - antes: `where municipio_nome = 'VARGINHA'` cravado no model, exigindo
      editar SQL para cada cliente novo;
    - antes: `cnae_descricao ilike '%ti%'`, que casa com "A-TI-vidades" e
      portanto com boa parte dos CNAEs do país — o filtro de segmento era,
      na prática, inexistente. Agora o casamento é por CÓDIGO CNAE.
    - agora existe lista de EXCLUSÃO (um escritório de contabilidade não quer
      prospectar outro escritório de contabilidade).
*/

with eventos as (
    select * from {{ ref('evt_new_company') }}
),

icp as (
    select * from {{ source('config', 'icp') }}
    where icp_nome = '{{ var("icp_ativo") }}'
),

municipios_alvo as (
    select municipio from {{ source('config', 'icp_municipio') }}
    where icp_nome = '{{ var("icp_ativo") }}'
),

portes_alvo as (
    select porte from {{ source('config', 'icp_porte') }}
    where icp_nome = '{{ var("icp_ativo") }}'
),

cnae_cfg as (
    select cnae, prioridade from {{ source('config', 'icp_cnae') }}
    where icp_nome = '{{ var("icp_ativo") }}'
),

avaliado as (
    select
        e.*,
        i.icp_nome,
        i.oferta                                        as icp_oferta,
        i.recencia_dias,
        i.score_minimo,
        i.peso_recencia, i.peso_cnae, i.peso_porte,
        i.peso_localizacao, i.peso_capital, i.peso_contexto,

        coalesce(c.prioridade, 'nenhum')                as cnae_prioridade,
        (e.municipio_nome in (select municipio from municipios_alvo)) as match_municipio,
        (e.porte in (select porte from portes_alvo))    as match_porte,

        -- Recência medida contra o fim da COMPETÊNCIA, não contra a data de
        -- execução. Com current_date, reprocessar 2024-08 em 2026 dava ~770
        -- dias e o filtro de recência do ICP (45 dias) zerava o Gold —
        -- silenciosamente, porque tabela vazia não é erro.
        date_diff('day', e.event_date, {{ data_referencia() }})
                                                        as dias_desde_evento

    from eventos e
    cross join icp i
    left join cnae_cfg c on c.cnae = e.cnae_principal
)

select *
from avaliado
where
    -- 1. Exclusões explícitas do ICP têm precedência sobre tudo
    cnae_prioridade <> 'excluido'

    -- 2. Geografia
    and match_municipio

    -- 3. Setor: precisa ser alvo primário ou secundário
    and cnae_prioridade in ('primario', 'secundario')

    -- 4. Recência (janela definida no ICP, não fixa no código)
    and dias_desde_evento is not null
    and dias_desde_evento <= recencia_dias
