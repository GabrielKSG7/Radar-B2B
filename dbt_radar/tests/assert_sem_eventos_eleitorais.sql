-- Campanhas e diretórios partidários (naturezas 4090 e 3271) não são abertura
-- de negócio e não podem entrar no Event Store. Em 2024-08 eles eram 36% dos
-- eventos de MG; sem este teste o filtro do evt_new_company poderia sumir numa
-- refatoração sem ninguém notar.
select event_id, cnpj, natureza_juridica
from {{ ref('evt_new_company') }}
where natureza_juridica in ('4090', '3271')
