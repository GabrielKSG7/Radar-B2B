{{ config(materialized='table') }}

/*
  EVENTO NEW_COMPANY — detecção por SET DIFFERENCE entre competências.

  Antes (V3): filtrava `data_inicio_atividade >= current_date - 30`.
  Problemas daquela abordagem:
    - não é idempotente (o resultado mudava conforme o dia da execução);
    - perdia empresas que entram na base com data retroativa;
    - não deixava fundação para nenhum outro evento de mudança.

  Agora: uma empresa é NOVA quando o CNPJ existe na competência atual e NÃO
  existe na anterior. Esta é a mesma mecânica que sustentará STATUS_CHANGED,
  ADDRESS_CHANGED, PARTNER_* etc. na expansão do catálogo.

  Preenche o esquema completo do Event Store previsto no Plano Diretor:
  event_id, cnpj, event_type, event_date, detected_at, source_competence,
  previous_value, current_value, confidence, event_metadata.
*/

with atual as (
    select * from {{ ref('slv_estabelecimentos') }}
    where competencia = '{{ var("competencia_atual") }}'
),

anterior as (
    select cnpj from {{ ref('slv_estabelecimentos') }}
    where competencia = '{{ var("competencia_anterior") }}'
),

novas as (
    select a.*
    from atual a
    where a.situacao_cadastral = '02'          -- apenas estabelecimentos ativos
      and not exists (select 1 from anterior p where p.cnpj = a.cnpj)
),

enriquecido as (
    select
        n.*,
        e.razao_social,
        e.capital_social,
        e.porte,
        e.porte_descricao,
        mun.descricao as municipio_nome,
        cna.descricao as cnae_descricao
    from novas n
    left join {{ ref('slv_empresas') }} e
           on e.cnpj_basico = n.cnpj_basico
          and e.competencia = n.competencia
    left join {{ ref('slv_dominios') }} mun
           on mun.dominio = 'MUNICIPIO' and mun.codigo = n.id_municipio
    left join {{ ref('slv_dominios') }} cna
           on cna.dominio = 'CNAE' and cna.codigo = n.cnae_principal
)

select
    -- Determinístico e estável: reprocessar não gera event_id diferente.
    md5(cnpj || '|NEW_COMPANY|' || '{{ var("competencia_atual") }}') as event_id,
    cnpj,
    cnpj_basico,
    'NEW_COMPANY'                                   as event_type,
    data_inicio_atividade                           as event_date,
    current_date                                    as detected_at,
    '{{ var("competencia_atual") }}'                as source_competence,
    '{{ var("competencia_anterior") }}'             as compared_competence,

    -- Para NEW_COMPANY não há valor anterior: a entidade não existia.
    cast(null as varchar)                           as previous_value,
    coalesce(razao_social, nome_fantasia, cnpj)     as current_value,

    /*
      Confiança da DETECÇÃO (não da IA). Alta quando a data declarada de
      início é coerente com o aparecimento na base; média quando a empresa
      surge com data muito antiga — pode ser inclusão retroativa/correção
      cadastral, não abertura recente de fato.
    */
    case
        when data_inicio_atividade is null then 'baixa'
        when data_inicio_atividade >= {{ data_referencia() }} - interval 120 day
            then 'alta'
        else 'media'
    end                                             as confidence,

    -- Contexto de negócio
    razao_social,
    nome_fantasia,
    municipio_nome,
    uf,
    cnae_principal,
    cnae_descricao,
    porte,
    porte_descricao,
    capital_social,
    email,
    telefone,

    -- Payload extensível do Event Store
    json_object(
        'situacao_cadastral', situacao_cadastral,
        'id_municipio', id_municipio,
        'bairro', bairro
    )                                               as event_metadata

from enriquecido
