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

-- geografia.uf e eventos do YAML, que antes eram decorativos. Lista vazia
-- significa "sem restrição" — nunca "nada passa" (REVISAO_PD_V2.md §3.3).
ufs_alvo as (
    select uf from {{ source('config', 'icp_uf') }}
    where icp_nome = '{{ var("icp_ativo") }}'
),

eventos_alvo as (
    select event_type from {{ source('config', 'icp_evento') }}
    where icp_nome = '{{ var("icp_ativo") }}'
),

ddds_alvo as (
    select ddd from {{ source('config', 'icp_ddd') }}
    where icp_nome = '{{ var("icp_ativo") }}'
),

padroes_contador as (
    select padrao from {{ source('config', 'icp_padrao_contador') }}
    where icp_nome = '{{ var("icp_ativo") }}'
),

-- Provedores de e-mail gratuitos. Para eles o que identifica um intermediário
-- (escritório que cadastra vários clientes com o mesmo e-mail) é o ENDEREÇO;
-- para domínio corporativo é o DOMÍNIO inteiro.
provedores_livres as (
    select unnest([
        'gmail.com', 'gmail.com.br', 'googlemail.com', 'hotmail.com', 'hotmail.com.br',
        'outlook.com', 'outlook.com.br', 'live.com', 'msn.com',
        'yahoo.com', 'yahoo.com.br', 'icloud.com', 'me.com',
        'uol.com.br', 'bol.com.br', 'terra.com.br', 'ig.com.br',
        'globo.com', 'globomail.com', 'r7.com', 'zipmail.com.br',
        'protonmail.com', 'proton.me'
    ]) as dominio
),

-- ÂNCORA DE RECÊNCIA. A Receita publica o snapshot no meio do mês: o de
-- 2026-11 termina em 15/11, não em 30/11. Medir a idade contra o fim da
-- competência envelhecia todo evento em ~15 dias e achatava o score de
-- recência. A âncora é o último dia COM VOLUME REAL de eventos (o último dia
-- do snapshot vem parcial: 17 eventos em 15/11 contra ~500 num domingo
-- comum), limitada ao fim da competência. Datas com poucos eventos — lixo de
-- cadastro ou dia de corte — não puxam a âncora. Se nenhum dia atinge o
-- mínimo (fixtures pequenas), usa o maior event_date; sem datas, o fim da
-- competência (comportamento anterior). Idempotente: depende só dos dados.
ancora as (
    select coalesce(
        max(event_date) filter (where n >= {{ var('ancora_min_eventos', 10) }}),
        max(event_date),
        {{ data_referencia() }}
    ) as data_ancora
    from (
        select event_date, count(*) as n
        from eventos
        where event_date <= {{ data_referencia() }}
        group by 1
    )
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
        i.pen_contador_atual, i.pen_fora_da_regiao, i.pen_grupo_empresarial,
        i.limiar_compartilhamento, i.limiar_dominio_intermediario,

        coalesce(c.prioridade, 'nenhum')                as cnae_prioridade,
        (e.municipio_nome in (select municipio from municipios_alvo)) as match_municipio,
        (e.porte in (select porte from portes_alvo))    as match_porte,

        -- Recência medida contra a ÂNCORA (último dia real do snapshot), não
        -- contra a data de execução nem contra o fim do mês. Com current_date,
        -- reprocessar 2024-08 em 2026 dava ~770 dias e o filtro de recência
        -- do ICP (45 dias) zerava o Gold — silenciosamente, porque tabela
        -- vazia não é erro.
        a.data_ancora                                   as data_ancora_recencia,
        date_diff('day', e.event_date, a.data_ancora)   as dias_desde_evento

    from eventos e
    cross join icp i
    cross join ancora a
    left join cnae_cfg c on c.cnae = e.cnae_principal
),

filtrado as (
    select *
    from avaliado
    where
        -- 1. Exclusões explícitas do ICP têm precedência sobre tudo
        cnae_prioridade <> 'excluido'

        -- 2. Geografia: UF (se o ICP declarar) e município
        and (not exists (select 1 from ufs_alvo)
             or uf in (select uf from ufs_alvo))
        and match_municipio

        -- 2b. Tipo de evento aceito pelo ICP
        and (not exists (select 1 from eventos_alvo)
             or event_type in (select event_type from eventos_alvo))

        -- 3. Setor: precisa ser alvo primário ou secundário
        and cnae_prioridade in ('primario', 'secundario')

        -- 4. Recência (janela definida no ICP, não fixa no código)
        and dias_desde_evento is not null
        and dias_desde_evento <= recencia_dias
),

-- Validade do contato. Telefone 4444444444 e e-mail sem forma de e-mail não
-- tornam o lead "acionável hoje"; antes contavam pontos de contexto.
contato as (
    select
        f.*,
        lower(trim(f.email))                            as email_norm,
        split_part(lower(trim(f.email)), '@', 2)        as email_dominio,
        regexp_matches(lower(trim(coalesce(f.email, ''))),
                       '^[^@\s]+@[^@\s]+\.[^@\s]+$')   as email_valido,
        regexp_replace(coalesce(f.telefone, ''), '[^0-9]', '', 'g') as tel_digitos
    from filtrado f
),

contato2 as (
    select
        c.*,
        substr(c.tel_digitos, 1, 2)                     as tel_ddd,
        substr(c.tel_digitos, 3)                        as tel_local,
        (
            length(c.tel_digitos) in (10, 11)
            and try_cast(substr(c.tel_digitos, 1, 2) as int) between 11 and 99
            and substr(c.tel_digitos, 3, 1) between '2' and '9'
            -- todos os dígitos iguais (44444444, 99999999) = telefone de mentira
            and length(replace(substr(c.tel_digitos, 3),
                               substr(c.tel_digitos, 3, 1), '')) > 0
        )                                               as telefone_valido,
        (split_part(lower(trim(c.email)), '@', 2)
            in (select dominio from provedores_livres))  as contato_provedor_livre,
        -- Domínio educacional (.edu.br) não identifica intermediário: é a
        -- instituição, não um escritório. Sem chave = sem sinal de
        -- compartilhamento.
        case when split_part(lower(trim(c.email)), '@', 2) like '%.edu.br'
                  or split_part(lower(trim(c.email)), '@', 2) = 'edu.br'
             then null
             when split_part(lower(trim(c.email)), '@', 2)
                  in (select dominio from provedores_livres)
             then lower(trim(c.email))
             else split_part(lower(trim(c.email)), '@', 2)
        end                                             as chave_contato
    from contato c
),

-- Quantas empresas da base inteira (competência atual) usam o mesmo e-mail
-- (provedor gratuito) ou domínio (corporativo). Só para as chaves dos
-- candidatos, para não agregar a base toda sem necessidade.
chaves as (
    select distinct chave_contato as chave from contato2 where email_valido
),

compartilhamento as (
    select chave, count(distinct cnpj_basico) as n_empresas
    from (
        select
            e.cnpj_basico,
            case when split_part(lower(trim(e.email)), '@', 2)
                      in (select dominio from provedores_livres)
                 then lower(trim(e.email))
                 else split_part(lower(trim(e.email)), '@', 2)
            end as chave
        from {{ ref('slv_estabelecimentos') }} e
        where e.competencia = '{{ var("competencia_atual") }}'
          and e.email is not null
    ) t
    where chave in (select chave from chaves)
    group by chave
),

sinalizado as (
    select
        c.*,
        coalesce(k.n_empresas, 0)                       as n_empresas_contato,

        -- SINAL 1 — provável contador atual.
        (c.email_valido and exists (
            select 1 from padroes_contador p
            where c.email_dominio like '%' || p.padrao || '%'
        ))                                              as contador_por_padrao,
        -- Compartilhamento só vira "contador" em dois casos: (a) o MESMO
        -- endereço de provedor gratuito em >= limiar empresas; (b) um domínio
        -- corporativo em >= limiar_dominio_intermediario empresas (centenas de
        -- empresas num domínio é plataforma/escritório, não grupo de lojas).
        (c.email_valido
         and c.limiar_compartilhamento > 0
         and coalesce(k.n_empresas, 0) >= c.limiar_compartilhamento
         and (c.contato_provedor_livre
              or (c.limiar_dominio_intermediario > 0
                  and coalesce(k.n_empresas, 0) >= c.limiar_dominio_intermediario))
        )                                               as contador_por_compartilhamento,
        -- Domínio corporativo compartilhado por poucas empresas: provável
        -- grupo empresarial (a decisão contábil fica fora da empresa nova).
        (c.email_valido
         and c.limiar_compartilhamento > 0
         and not c.contato_provedor_livre
         and coalesce(k.n_empresas, 0) >= c.limiar_compartilhamento
        )                                               as dominio_compartilhado,

        -- SINAL 2 — telefone válido com DDD fora da região-alvo.
        (c.telefone_valido
         and exists (select 1 from ddds_alvo)
         and c.tel_ddd not in (select ddd from ddds_alvo)
        )                                               as sinal_fora_da_regiao,

        -- SINAL 3 — filial (ordem do CNPJ <> 0001) ou SPE.
        (substr(c.cnpj, 9, 4) <> '0001')                as sinal_filial,
        regexp_matches(upper(coalesce(c.razao_social, '')),
                       '(^|[^A-Z])SPE([^A-Z]|$)')       as sinal_spe

    from contato2 c
    left join compartilhamento k on k.chave = c.chave_contato
)

select
    * exclude (email_norm, tel_digitos, tel_ddd, tel_local, chave_contato,
               dominio_compartilhado),
    (contador_por_padrao or contador_por_compartilhamento) as sinal_contador_atual,
    (dominio_compartilhado
     and not contador_por_padrao
     and not contador_por_compartilhamento)                as grupo_por_dominio,
    (sinal_filial or sinal_spe
     or (dominio_compartilhado
         and not contador_por_padrao
         and not contador_por_compartilhamento))           as sinal_grupo_empresarial
from sinalizado
