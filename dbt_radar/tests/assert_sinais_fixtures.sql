-- Verifica os sinais do score e a âncora de recência contra as 20 empresas
-- sintéticas de `scripts/gerar_fixtures.py::casos_sinais`.
--
-- Só olha para linhas cuja razão social traz a marca "FIXTURE SINTETICO", que
-- não existe em dado real da Receita: contra a base de verdade este teste não
-- encontra nada e passa. Cada linha devolvida é uma violação.

with fixtures as (
    select
        o.*,
        cast(regexp_extract(razao_social, 'SINTETICO ([0-9]{2})', 1) as int) as caso
    from {{ ref('gld_opportunities') }} o
    where razao_social like '%FIXTURE SINTETICO%'
),

-- caso, contador atual?, fora da região?, grupo empresarial?
esperado as (
    select * from (values
        ( 1, false, false, false),   -- limpo
        ( 2, true,  false, false),   -- domínio com "contab"
        ( 3, false, false, true ),   -- 5 empresas no mesmo domínio corporativo: grupo
        ( 4, false, false, true ),
        ( 5, false, false, true ),
        ( 6, false, false, true ),
        ( 7, false, false, true ),
        ( 8, false, false, true ),   -- filial (ordem 0002)
        ( 9, false, false, true ),   -- SPE
        (10, false, true,  false),   -- DDD 11
        (11, false, false, false),   -- telefone 99999999: inválido, não é "fora da região"
        (12, true,  false, false),   -- 5 empresas no mesmo e-mail gratuito
        (13, true,  false, false),
        (14, true,  false, false),
        (15, true,  false, false),
        (16, true,  false, false),
        (17, false, false, false),   -- e-mail gratuito usado por uma só empresa
        (18, true,  false, false),   -- domínio em >= 50 empresas: plataforma
        (19, false, false, false),   -- .edu.br compartilhado: sem sinal
        (20, false, false, false)    -- gmail.com.br: endereço único, sem sinal
    ) t(caso, contador, regiao, grupo)
)

select f.caso, 'sinal divergente do esperado' as violacao
from fixtures f
join esperado e using (caso)
where f.sinal_contador_atual    <> e.contador
   or f.sinal_fora_da_regiao    <> e.regiao
   or f.sinal_grupo_empresarial <> e.grupo

union all
-- Âncora: as 20 empresas abrem no mesmo dia (fim da competência - 10 dias),
-- com volume suficiente para ser a âncora. Se ela caísse no fim do mês, a
-- idade seria 10 e não 0.
select caso, 'âncora de recência não está no último dia com volume'
from fixtures
where dias_desde_evento <> 0
   or data_ancora_recencia <> ({{ data_referencia() }} - interval 10 day)::date

union all
-- Contato válido: o telefone 99999999 não dá pontos de contexto.
select caso, 'telefone inválido pontuou como contato'
from fixtures
where (caso = 11 and json_extract(score_fatores, '$.contexto')::int
                     >= (select json_extract(score_fatores, '$.contexto')::int
                         from fixtures where caso = 1))

union all
-- Texto do digest com acento: "CNAE primário", não "primario".
select caso, 'explicação do score sem acento em "primário"'
from fixtures
where score_explanation like '%CNAE primario%'

union all
select 0, 'casos de sinais faltando no Gold'
where exists (select 1 from {{ ref('evt_new_company') }}
              where razao_social like '%FIXTURE SINTETICO%')
  and (select count(*) from fixtures) <> 20
