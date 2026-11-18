-- Filial nova de uma empresa que já existia na competência anterior não é
-- empresa nascendo: não pode estar em evt_new_company. Devolve as violações.
select e.cnpj
from {{ ref('evt_new_company') }} e
where substr(e.cnpj, 1, 8) in (
    select cnpj_basico
    from {{ ref('slv_estabelecimentos') }}
    where competencia = '{{ var("competencia_anterior") }}'
)
