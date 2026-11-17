{{ config(materialized='table') }}

/*
  OPPORTUNITY SCORE — determinístico, ponderado pelo ICP e auditável.

  Correções em relação à V3:
    - antes: base fixa 50 + recência, resultando em apenas TRÊS valores
      possíveis (50, 65, 85). A "base ICP" era constante para todos que
      passavam no filtro, logo não discriminava nada.
    - antes: `where opportunity_score >= 50` era inerte (o mínimo já era 50).
    - antes: a explicação era uma string concatenada, impossível de calibrar.

    - agora: seis fatores, cada um com peso vindo do ICP, somando até 100.
    - agora: os fatores são persistidos em JSON (score_fatores), permitindo
      auditar, recalibrar e explicar sem reprocessar.
*/

with matches as (
    select * from {{ ref('gld_icp_matches') }}
),

fatores as (
    select
        m.*,

        -- 1. RECÊNCIA — proporcional dentro da janela do ICP.
        --    Uma empresa aberta ontem vale o peso inteiro; no limite da
        --    janela, zero. Decaimento linear, limitado a [0, peso]: um
        --    evento datado depois da referência (dias negativos) não pode
        --    render mais que o peso inteiro.
        round(
            m.peso_recencia
            * least(1.0, greatest(0.0, 1.0 - (m.dias_desde_evento::double
                                              / nullif(m.recencia_dias, 0))))
        )::int                                          as pts_recencia,

        -- 2. ADERÊNCIA DE CNAE
        case m.cnae_prioridade
            when 'primario'   then m.peso_cnae
            when 'secundario' then round(m.peso_cnae * 0.6)::int
            else 0
        end                                             as pts_cnae,

        -- 3. PORTE
        case when m.match_porte then m.peso_porte else 0 end as pts_porte,

        -- 4. LOCALIZAÇÃO
        case when m.match_municipio then m.peso_localizacao else 0 end as pts_localizacao,

        -- 5. CAPITAL SOCIAL — proxy de capacidade de investimento.
        case
            when m.capital_social is null       then 0
            when m.capital_social >= 100000     then m.peso_capital
            when m.capital_social >=  50000     then round(m.peso_capital * 0.7)::int
            when m.capital_social >=  10000     then round(m.peso_capital * 0.4)::int
            else 0
        end                                             as pts_capital,

        -- 6. CONTEXTO — completude do cadastro. Um lead com e-mail e
        --    telefone é acionável hoje; sem contato, exige garimpo.
        (
            case when m.email    is not null then round(m.peso_contexto * 0.4)::int else 0 end
          + case when m.telefone is not null then round(m.peso_contexto * 0.4)::int else 0 end
          + case when m.confidence = 'alta'  then round(m.peso_contexto * 0.2)::int else 0 end
        )                                               as pts_contexto

    from matches m
),

pontuado as (
    select
        *,
        least(100,
            pts_recencia + pts_cnae + pts_porte
          + pts_localizacao + pts_capital + pts_contexto
        )                                               as opportunity_score
    from fatores
)

select
    event_id,
    cnpj,
    event_type,
    event_date,
    detected_at,
    source_competence,
    confidence                                          as detection_confidence,
    icp_nome,
    icp_oferta,

    razao_social,
    nome_fantasia,
    municipio_nome,
    uf,
    cnae_principal,
    cnae_descricao,
    porte_descricao,
    capital_social,
    email,
    telefone,
    dias_desde_evento,
    cnae_prioridade,

    opportunity_score,

    -- Fatores estruturados: a fonte da verdade da explicação.
    json_object(
        'recencia',     pts_recencia,
        'cnae',         pts_cnae,
        'porte',        pts_porte,
        'localizacao',  pts_localizacao,
        'capital',      pts_capital,
        'contexto',     pts_contexto
    )                                                   as score_fatores,

    -- Texto derivado dos fatores (nunca escrito à mão em outro lugar).
    concat_ws(' · ',
        'Aberta há ' || dias_desde_evento || ' dias (+' || pts_recencia || ')',
        'CNAE ' || cnae_prioridade || ' (+' || pts_cnae || ')',
        case when pts_porte > 0 then porte_descricao || ' (+' || pts_porte || ')' end,
        case when pts_localizacao > 0 then municipio_nome || ' (+' || pts_localizacao || ')' end,
        case when pts_capital > 0 then 'Capital social (+' || pts_capital || ')' end,
        case when pts_contexto > 0 then 'Contato disponível (+' || pts_contexto || ')' end
    )                                                   as score_explanation,

    current_timestamp                                   as scored_at

from pontuado
where opportunity_score >= score_minimo
