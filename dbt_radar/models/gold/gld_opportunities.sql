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

  Três AJUSTES NEGATIVOS (2.5.0), também vindos do ICP e também persistidos em
  score_fatores: provável contador atual, DDD fora da região e grupo
  empresarial (filial/SPE). O score é a soma dos fatores e dos ajustes,
  limitada a [0, 100].
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
        --    Só conta contato VÁLIDO: telefone 4444444444 ou e-mail sem forma
        --    de e-mail não deixam o lead acionável.
        (
            case when m.email_valido     then round(m.peso_contexto * 0.4)::int else 0 end
          + case when m.telefone_valido  then round(m.peso_contexto * 0.4)::int else 0 end
          + case when m.confidence = 'alta' then round(m.peso_contexto * 0.2)::int else 0 end
        )                                               as pts_contexto,

        -- AJUSTES NEGATIVOS (pontos descontados, vindos do ICP).
        case when m.sinal_contador_atual
             then -m.pen_contador_atual else 0 end      as aj_contador,
        case when m.sinal_fora_da_regiao
             then -m.pen_fora_da_regiao else 0 end      as aj_regiao,
        case when m.sinal_grupo_empresarial
             then -m.pen_grupo_empresarial else 0 end   as aj_grupo

    from matches m
),

pontuado as (
    select
        *,
        least(100, greatest(0,
            pts_recencia + pts_cnae + pts_porte
          + pts_localizacao + pts_capital + pts_contexto
          + aj_contador + aj_regiao + aj_grupo
        ))                                              as opportunity_score
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
    natureza_juridica,
    capital_social,
    email,
    telefone,
    dias_desde_evento,
    data_ancora_recencia,
    cnae_prioridade,

    -- Evidências dos ajustes (para auditar e calibrar com o feedback).
    sinal_contador_atual,
    contador_por_padrao,
    contato_provedor_livre,
    grupo_por_dominio,
    n_empresas_contato,
    sinal_fora_da_regiao,
    sinal_grupo_empresarial,

    opportunity_score,

    -- Fatores estruturados: a fonte da verdade da explicação.
    json_object(
        'recencia',     pts_recencia,
        'cnae',         pts_cnae,
        'porte',        pts_porte,
        'localizacao',  pts_localizacao,
        'capital',      pts_capital,
        'contexto',     pts_contexto,
        'ajuste_contador', aj_contador,
        'ajuste_regiao',   aj_regiao,
        'ajuste_grupo',    aj_grupo
    )                                                   as score_fatores,

    -- Texto derivado dos fatores (nunca escrito à mão em outro lugar).
    concat_ws(' · ',
        'Aberta há ' || dias_desde_evento || ' dias (+' || pts_recencia || ')',
        'CNAE ' || case cnae_prioridade when 'primario' then 'primário'
                                        when 'secundario' then 'secundário'
                                        else cnae_prioridade end
               || ' (+' || pts_cnae || ')',
        case when pts_porte > 0 then porte_descricao || ' (+' || pts_porte || ')' end,
        case when pts_localizacao > 0 then municipio_nome || ' (+' || pts_localizacao || ')' end,
        case when pts_capital > 0 then 'Capital social (+' || pts_capital || ')' end,
        case when pts_contexto > 0 then 'Contato disponível (+' || pts_contexto || ')' end,
        case when aj_contador < 0 then
            'Provável contador atual (' || aj_contador || ': '
            || case when contador_por_padrao
                    then 'domínio de e-mail de contabilidade'
                    when contato_provedor_livre
                    then 'e-mail compartilhado com ' || n_empresas_contato || ' empresas'
                    else 'domínio de e-mail compartilhado com ' || n_empresas_contato
                         || ' empresas (provável intermediário)' end
            || ')' end,
        case when aj_regiao < 0 then 'DDD fora da região (' || aj_regiao || ')' end,
        case when aj_grupo < 0 then
            'Provável grupo empresarial (' || aj_grupo || ': '
            || concat_ws(' + ',
                   case when sinal_filial then 'filial' end,
                   case when sinal_spe then 'SPE' end,
                   case when grupo_por_dominio
                        then 'domínio de e-mail compartilhado com '
                             || n_empresas_contato || ' empresas' end)
            || ' — decisão contábil provavelmente fora da empresa)' end
    )                                                   as score_explanation,

    current_timestamp                                   as scored_at

from pontuado
where opportunity_score >= score_minimo
