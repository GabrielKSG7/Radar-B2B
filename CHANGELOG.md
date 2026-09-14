# Changelog — Radar B2B

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/).

---

## [2.1.7] — 2026-09-14

Com as correções da 2.1.6 o pipeline passou a produzir eventos: 580.729 em
2024-08. O `diagnostico_eventos.py` reprovou: 95,5% deles eram empresas
abertas anos antes, e a distribuição por ano de abertura reproduzia o perfil
etário da base ativa — assinatura de amostra, não de evento.

### Descoberto

- **A fatia não é estável entre competências.** Medido com a MESMA fatia 0 nas
  duas competências: **1.400.049 CNPJs (69,2%) existem só em 2024-07** e
  1.451.412 só em 2024-08 — de ~2 milhões em cada mês, apenas ~30% são comuns.
  Cadastro de empresa não evapora assim.

  O mesmo CNPJ muda de arquivo a cada publicação da Receita. Isso invalida a
  premissa registrada em `config.py` ("particionadas por hash do CNPJ
  básico"): duas competências ingeridas com "a fatia 0" são recortes
  diferentes do mesmo universo, e o set difference entre elas mede sorteio.

  Consequência: **não existe atalho para o CDC**. Medir abertura de empresa
  exige as 10 fatias nas duas competências. O
  `assert_evento_nao_existe_na_competencia_anterior` não protege contra isso —
  sob o critério dele as empresas faltantes são legitimamente ausentes.

### Alterado — ingestão fatia a fatia

- Carregar as 10 fatias como antes (baixar tudo, extrair tudo, depois carregar)
  exigiria manter **~45 GB de CSV extraído por competência** no disco, mais de
  100 GB somando as duas.
  → `ingerir` passa a processar **uma fatia por vez**: baixa, extrai, carrega,
  apaga o CSV e só então vai para a próxima. Pico de disco de poucos GB. Os
  ZIPs ficam, porque são o cache que evita rebaixar tudo.
  → `_carregar_parquet` ganhou `fatia` (grava `dados_<n>.parquet` dentro da
  partição) e `limpar` (só a primeira fatia zera a pasta). O Hive partitioning
  lê todos os parquet da partição, então o resultado é idêntico ao da carga
  única.
  → O limiar de rejeição é avaliado por fatia, enquanto o CSV ainda existe —
  antes o denominador era calculado depois, quando o arquivo já teria sido
  apagado.

- **Aviso de carga parcial.** Ingerir com menos que todas as fatias agora
  imprime um alerta explicando que o resultado serve para testar o
  encanamento e não para gerar digest.

- `run_pipeline.py`: `FATIAS` passa a ser `0..9`, com a medição que justifica
  a decisão registrada no próprio comentário.

### Adicionado

- 2 testes: um parquet por fatia somando o total na partição, e `limpar=False`
  preservando o que já foi carregado (com o caso inverso, para fixar o
  contrato).

### Verificação

| Verificação | Resultado |
|---|---|
| Testes pytest | 40 passando (antes: 38) |
| Lint (ruff) | limpo |
| Ingestão de 3 fatias (fixtures) | 3 parquet na partição, soma correta (861 = 3×287) |
| CSVs após a carga | pasta vazia — disco liberado |
| ZIPs após a carga | preservados (cache do `[SKIP]`) |

> A carga real das 10 fatias nas duas competências ainda não foi executada —
> são horas de download e dependem da infraestrutura da RFB. É o que fecha o
> item "primeira carga com dados reais" do roadmap, e o veredito é do
> `diagnostico_eventos.py`.

---

## [2.1.6] — 2026-09-14

O pipeline rodou de ponta a ponta, todas as etapas "✅ SUCESSO", digest
gerado — e **zero eventos detectados**. O produto inteiro entregou uma folha
em branco sem um único erro no caminho. Dois defeitos independentes, ambos da
mesma família: o pipeline media o tempo pelo relógio de parede em vez de pela
competência processada, e o orquestrador não dizia ao dbt qual competência
processar.

> Achado pelo `scripts/diagnostico_eventos.py`. Sem ele, a conclusão natural
> seria "não houve aberturas neste mês".

### Corrigido — CRÍTICO

- **O dbt transformava uma competência que não estava no banco.** O
  orquestrador rodava `dbt run --profiles-dir .` **sem `--vars`**, então valia
  o default do `dbt_project.yml` (`2026-08`/`2026-07`) enquanto a ingestão
  carregava `2024-08`/`2024-07`. Todo `where competencia = '2026-08'` casou
  com zero linhas. Os 10 modelos reportaram OK porque **construir uma tabela
  vazia é operação perfeitamente válida** — o dbt não tem como saber que o
  vazio não era o esperado.

  O comentário no próprio `dbt_project.yml` já avisava: *"Sobrescreva com
  --vars na execução."*
  → As competências viraram constantes no topo de `run_pipeline.py` e
  alimentam a ingestão **e** o dbt. Uma fonte, dois consumidores: fica
  impossível ingerir um mês e transformar outro.

- **Recência medida contra a data de execução.** `gld_icp_matches` calculava
  `date_diff('day', event_date, current_date)` e filtrava por
  `dias_desde_evento <= recencia_dias` (45, no ICP). Processando 2024-08 em
  setembro de 2026, uma empresa aberta em 05/08/2024 dava **770 dias** e era
  descartada. Mesmo com as `--vars` corrigidas, o Gold continuaria vazio.
  → Novo macro `data_referencia()`: âncora no último dia da competência
  processada. A mesma empresa passa a dar **26 dias** e casa corretamente.

  Isso também torna o cálculo **idempotente** — reprocessar a mesma
  competência daqui a um ano dá o mesmo score. É exatamente o defeito que o
  cabeçalho de `evt_new_company` já criticava na V3 ("o resultado mudava
  conforme o dia da execução") e que havia sobrevivido na camada Gold.

- **`confidence` sempre 'media'.** Mesmo `current_date` cravado em
  `evt_new_company`: com dado histórico, nenhum evento jamais alcançava a
  janela de 120 dias. O campo dizia a mesma coisa para tudo.
  → Passa a usar `data_referencia()`.

### Corrigido — ALTO

- **Fatias assimétricas entre competências.** `2024-07` era ingerida com
  `--fatias 0` e `2024-08` com `--fatias 0,1`. As fatias são partições por
  hash do CNPJ básico: as empresas da fatia 1 existem nos dois meses **na
  realidade**, mas só estavam no Bronze de um — e o set difference as
  classificaria como novas. O
  `assert_evento_nao_existe_na_competencia_anterior` **não pega isso**: sob o
  critério dele, essas empresas são legitimamente ausentes do mês anterior.
  → Constante `FATIAS`, única para as duas competências.

- **`dbt run` trocado por `dbt build`.** O `run` não executa os 24 testes de
  dados. O pipeline vinha entregando digest sem rodar
  `assert_evento_nao_existe_na_competencia_anterior`, que o README descreve
  como o teste sem o qual "o produto perde credibilidade". Se um teste falhar
  agora, o pipeline para — comportamento desejado.

### Adicionado

- `dbt_radar/macros/data_referencia.sql` — âncora temporal única, documentada.
- 3 testes de orquestração que executam o `main()` real com `run_step`
  interceptado, validando a orquestração em si e não uma cópia dela:
  competências consistentes entre ingestão e dbt, fatias idênticas, e uso de
  `dbt build`. Os três falham contra a versão anterior.

### Alterado

- `scripts/diagnostico_eventos.py` passa a tratar **zero eventos como falha**
  (código de saída 1), não como "nada a analisar", e lista as causas prováveis
  em ordem. Também deixou de imprimir `None -> None` quando não há eventos dos
  quais extrair a competência.
- `test_pipeline_carrega_icp_antes_do_dbt` não fixa mais o subcomando `dbt
  run` — ele quebrou sozinho na troca para `dbt build`.

### Verificação

| Verificação | Resultado |
|---|---|
| Testes pytest | 38 passando (antes: 35) |
| Os 3 testes de orquestração contra a versão anterior | falham, como esperado |
| Lint (ruff) | limpo |
| Comando dbt gerado | parseia como YAML válido, competências corretas |
| Âncora temporal | 2024-08 → `2024-08-31`; empresa de 05/08/2024 passa de 770 para 26 dias |
| Diagnóstico com banco vazio | reprova e aponta a causa certa |

> Ainda não verificado com dado real: quantos eventos surgem depois destas
> correções. É o próximo passo — e o `diagnostico_eventos.py` é quem responde.

---

## [2.1.5] — 2026-09-14

Ingestão, carga de ICP e **dbt completo** (`PASS=10, ERROR=0`, incluindo
`evt_new_company`, `gld_icp_matches` e `gld_opportunities`). O pipeline parou
na etapa seguinte, por forma de invocação.

### Corrigido — ALTO

- **Módulos de `src/` chamados como arquivo solto.** O orquestrador executava
  `python src/enriquecimento.py`, que carrega o arquivo fora do pacote e
  estoura no primeiro import relativo:
  `ImportError: attempted relative import with no known parent package`.
  `src/digest.py` tem o mesmo formato e falharia logo em seguida.

  A ingestão já era chamada certo (`python -m src.ingestao`) — a
  inconsistência estava só nas duas últimas etapas, e o README sempre
  documentou a forma correta.
  → `python -m src.enriquecimento` e `python -m src.digest`.

### Adicionado

- Dois testes de regressão, ambos verificados contra a versão anterior:
  - `test_pipeline_invoca_modulos_como_pacote` — nenhuma linha de código do
    orquestrador pode invocar `python src/<modulo>.py`. Ignora comentários,
    para que a forma errada possa ser citada na documentação do próprio
    arquivo sem reprovar o teste.
  - `test_modulos_com_import_relativo_sao_chamados_com_dash_m` — parametrizado
    por módulo: se o arquivo usa `from .`, o orquestrador tem de chamá-lo com
    `python -m`. Cobre automaticamente qualquer módulo novo de `src/`.

### Verificação

| Verificação | Resultado |
|---|---|
| Testes pytest | 35 passando (antes: 31) |
| Os 3 testes novos contra a versão anterior | falham, como esperado |
| Lint (ruff) | limpo |
| `python src/enriquecimento.py` | reproduz o ImportError |
| `python -m src.enriquecimento --help` | funciona |

> Sem `GEMINI_API_KEY` a etapa não falha: o módulo avisa
> (`[AVISO] GEMINI_API_KEY ausente`) e usa a heurística, marcando a
> procedência — comportamento já previsto no README.

---

## [2.1.4] — 2026-09-14

Com o Bronze finalmente correto (2,0 M e 2,6 M de estabelecimentos MG nas duas
competências), o pipeline avançou até o dbt e parou em
`Catalog Error: Table with name "config.icp" does not exist`.

### Corrigido — ALTO

- **O orquestrador pulava a carga do ICP.** `run_pipeline.py` ia da ingestão
  direto para o `dbt run`, mas o README sempre documentou um passo entre os
  dois: `python scripts/carregar_icp.py`, que materializa `config.icp`,
  `config.icp_cnae`, `config.icp_municipio` e `config.icp_porte` — lidas por
  `gld_icp_matches`.

  O pipeline vinha funcionando **por acidente**: a tabela sobrevivia dentro de
  `data/radar.duckdb` de alguma execução manual anterior. Recriar o banco
  (necessário na 2.1.2 para descartar o Bronze contaminado) revelou a
  dependência de estado residual — que quebraria igual em qualquer máquina
  limpa, no CI ou num `git clone`. Isso contraria o requisito de **pipeline
  idempotente** do Plano Diretor.
  → Passo incorporado ao orquestrador, antes do dbt. É idempotente
  (`CREATE OR REPLACE`), custa ~1 s e mantém o ICP em sincronia com o YAML a
  cada execução, em vez de congelado no que foi carregado um dia.

### Adicionado

- Teste `test_pipeline_carrega_icp_antes_do_dbt`: verifica que
  `run_pipeline.py` invoca `carregar_icp.py` **antes** do `dbt run`. Confirmado
  que falha contra a versão anterior.

### Alterado

- `run_pipeline.py` passa no ruff (9 avisos pré-existentes: ordenação de
  imports, espaços em branco, `result` e `e` atribuídos sem uso). O README já
  mandava incluí-lo no lint.
- Etapas renumeradas para 1A, 1B, 2 … 5.

### Verificação

| Verificação | Resultado |
|---|---|
| Testes pytest | 31 passando (antes: 30) |
| Teste novo contra a versão anterior | falha, como esperado |
| Lint (ruff), agora incluindo `run_pipeline.py` | limpo |
| `carregar_icp.py` em banco sem o schema | cria as 4 tabelas (1 ICP, 9 CNAEs, 3 municípios, 2 portes) |

### Pendente — decisão do Gabriel

- O orquestrador roda `dbt run`, que **não executa os 24 testes de dados**. O
  README define `dbt build` como o comando de qualidade, e é o `build` que
  roda `assert_evento_nao_existe_na_competencia_anterior` — descrito no próprio
  README como o teste sem o qual "o produto perde credibilidade". Trocar
  `run` por `build` faz o pipeline validar o que produziu; não foi feito aqui
  para não misturar uma mudança de comportamento com a correção do bloqueio.

---

## [2.1.3] — 2026-09-14

A 2.1.2 declarou `encoding='latin-1'` por todo o pipeline — o que a cartilha
manda e o que a maior parte da documentação sobre os Dados Abertos do CNPJ
repete. Estava **incompleto**: a carga passou a abortar com
`Invalid Input Error: File is not latin-1 encoded` em `Estabelecimentos`.

### Corrigido — CRÍTICO

- **A fonte não é latin-1; é Windows-1252.** Os arquivos trazem bytes
  `0x80–0x9F`, faixa que o ISO-8859-1 reserva para controle e que o
  Windows-1252 usa para aspas curvas (`“ ”`), travessão (`–`), reticências
  (`…`) e apóstrofo (`’`) — resíduo de texto digitado no Windows, corriqueiro
  em `nome_fantasia`. Tabelas de domínio (vocabulário controlado) não têm
  esses bytes; os arquivos de texto livre têm.

  O leitor CSV do DuckDB não resolve isso em nenhuma configuração:

  | Tentativa | Resultado |
  |---|---|
  | `encoding='latin-1'` | aborta: *"File is not latin-1 encoded"* |
  | `encoding='latin-1'` + `ignore_errors` | aborta igual — é falha dura, não rejeição de linha |
  | `encoding='cp1252'` / `'windows-1252'` / `'iso-8859-1'` | *"The CSV Reader does not support the encoding"* (só aceita utf-8, utf-16, latin-1) |
  | `encoding='utf-8'` | erro interno e, em seguida, **conexão invalidada** |

  → A normalização passou para a **extração** (`extrair`): o conteúdo sai do
  ZIP convertido de cp1252 para **UTF-8**, e daí para frente o pipeline inteiro
  lê UTF-8. Como a extração já grava o arquivo, converter em streaming **não
  custa I/O adicional**. cp1252 é single-byte, então nenhum caractere cruza a
  fronteira entre blocos.

  Ganho colateral: `“ ”` e `–` são **preservados** como caracteres, em vez de
  virarem `?` ou serem trocados por aspas ASCII — o que, sendo `"` o caractere
  de aspas do CSV, corromperia a própria estrutura do arquivo.

### Alterado

- `extrair` passa a gravar o membro pelo **nome base** em vez de recriar a
  árvore interna do ZIP. Os arquivos da RFB são planos; de quebra, elimina
  o risco de *zip slip*.
- `ENCODING_RFB` agora significa "como o pipeline grava e lê"
  (`utf-8`); `ENCODING_FONTE` (`cp1252`) descreve a origem.

### Adicionado

- **Fixtures passam a ser gravadas em cp1252**, com um CNAE contendo aspa
  curva e travessão. Conferido: contra a 2.1.2 essas fixtures reproduzem o
  erro exato (*"File is not latin-1 encoded"*); contra esta versão, carregam
  preservando os caracteres.
- 2 testes: `extrair` devolve UTF-8 válido com os caracteres certos, e o
  caminho completo ZIP → extração → carga com bytes C1.

### Verificação

| Verificação | Resultado |
|---|---|
| Testes pytest | 30 passando (antes: 28) |
| Lint (ruff) | limpo |
| Fixtures cp1252 contra a 2.1.2 | falham com o erro original — regressão coberta |
| Ingestão end-to-end com `--uf MG` | 287 estabelecimentos, 14 CNAEs |
| Caracteres preservados | `Comércio de peças “genuínas” – sob encomenda` |

> Diagnóstico apoiado em varredura dos 256 valores de byte no leitor do
> DuckDB: sob `latin-1` ele recusa exatamente a faixa `0x80–0x9F`.

---

## [2.1.2] — 2026-09-14

A carga com **dados reais** quebrava em `Estabelecimentos` e — pior —
**tinha sucesso aparente com dados errados** nas demais tabelas. Três defeitos
independentes de parsing, todos na mesma origem: o código lia os arquivos da
Receita num formato que não é o deles.

> **Regressão de processo.** As correções descritas na 2.1.1 (leitura linha a
> linha dos domínios; `store_rejects`) **não estavam mais no código**:
> `src/ingestao.py` foi reescrito em 13/09 e as perdeu, enquanto o CHANGELOG
> seguia afirmando que existiam. A suíte não acusou nada — ver
> *Corrigido — testes que não podiam falhar*, abaixo.

### Corrigido — CRÍTICO

- **Encoding errado: linhas acentuadas sumiam em silêncio.** Os arquivos da
  Receita são `latin-1` (cartilha §2.1, "pegadinhas da fonte"), mas a carga
  não declarava encoding e o DuckDB assumia UTF-8. Bytes acentuados são
  sequências UTF-8 inválidas; com `ignore_errors=true`, **a linha inteira era
  descartada sem aviso**. No arquivo real de CNAEs: **145 das 1.359 linhas
  carregadas** — exatamente as 145 que não têm um único acento. Um CNAE
  ausente é uma empresa que deixa de casar com o ICP.
  → `encoding='latin-1'` explícito em todas as leituras.

- **`quote=''`: aspas entravam no dado e o filtro de UF nunca casava.** Os
  campos da Receita vêm entre aspas duplas (`"0111301";"Cultivo de arroz"`).
  Com as aspas desligadas, o valor lido era `"MG"` — com as aspas — e
  `WHERE column19 = 'MG'` não casava com nada. Era essa a causa de
  `estabelecimentos  0 linhas` numa competência inteira, sem erro algum.
  As demais tabelas carregavam "com sucesso" com **todos os valores
  poluídos por aspas** (`"ALPHA LTDA"`, `"0,00"`).
  → `quote='"'`, que também faz um `;` **dentro** de campo
  (`"LOJA A; LOJA B"`) parar de virar coluna extra — origem do
  `Error when sniffing file ... ESTABELE` / *"columns are set as 30, sniffer
  found 31"* que travava a ingestão.

- **Contador de rejeições fixo em zero.** `_carregar_parquet` terminava com
  `return inseridas, 0`. Toda a guarda de `LIMIAR_REJEICAO` — que deveria
  abortar a carga quando o layout muda — era **código morto**: nenhuma
  rejeição jamais era contada, então a razão nunca passava de 0%.
  → `store_rejects=true` com contagem real. Conta **linhas distintas**
  (`COUNT(DISTINCT (file_id, line))`) e não registros de erro: o DuckDB emite
  um erro por coluna excedente, e somá-los inflaria a razão a ponto de
  disparar o limiar numa base saudável.

### Corrigido — ALTO

- **Sniffer abortava o arquivo inteiro.** Mesmo com o dialeto correto, o
  DuckDB ainda tentava deduzi-lo e falhava quando as linhas ruins confundiam a
  amostra — derrubando a fatia inteira em vez de rejeitar as linhas.
  → `auto_detect=false`. Já declaramos delimitador, aspas, encoding e colunas;
  não há o que adivinhar. O pior caso passa a ser "tudo rejeitado e
  contabilizado" (diagnosticável) em vez de uma exceção opaca.

- **Domínios voltaram a ser lidos linha a linha** (técnica da 2.1.1, perdida
  na reescrita): delimitador `\x07` e corte no primeiro `;`. Sem
  `ignore_errors`, de propósito — aqui perder uma linha é perder um CNAE.

- **Colisão de `reject_scans` entre tabelas.** `store_rejects` materializa
  duas tabelas; nomear só a de erros fazia a segunda chamada na mesma conexão
  falhar com *"Reject Scan Table name reject_scans is already in use"*.
  → `rejects_scan` nomeado por tabela.

- **Limiar de rejeição media contra o denominador errado.** Comparava
  rejeitadas contra *carregadas*; com `--uf MG`, "carregadas" é só Minas
  enquanto as rejeições vêm do país inteiro, e a razão não significa nada.
  → Passa a medir contra o total de linhas do arquivo, contado apenas
  **quando há alguma rejeição** (no caminho saudável, custo zero).

### Corrigido — testes que não podiam falhar

A suíte passava **21/21 contra o código quebrado**. Dois testes eram
decorativos:

- `test_dominio_com_ponto_e_virgula_na_descricao` reimplementava o SQL
  **dentro do próprio teste**, validando a técnica e não o código de
  produção. Quando `ingestao.py` perdeu a técnica, o teste seguiu verde.
- `test_limiar_de_rejeicao_configurado` só verificava o **valor da constante**
  (`0 < LIMIAR <= 0.05`), nunca que rejeições fossem contadas — passava
  tranquilamente com `return inseridas, 0`.

→ 7 testes novos que chamam as **funções de produção** e falham contra o
código anterior: linha de domínio preservada, acento preservado, `;` dentro de
campo, filtro de UF com valor entre aspas, rejeição contabilizada, uma linha
ruim = uma rejeição, e arquivo todo malformado sem aborto de sniffing.

### Adicionado

- **Fixtures com acento.** As fixtures eram ASCII puro ("TRES CORACOES",
  "Comercio"), e em ASCII `latin-1` e UTF-8 são byte a byte idênticos — por
  isso o CI passava enquanto o dado real quebrava. Municípios, CNAEs e razões
  sociais agora carregam acentos, e ~5% dos nomes fantasia carregam um `;`
  dentro do campo. Um comentário no arquivo explica que os acentos não são
  decoração.

### Verificação

| Verificação | Resultado |
|---|---|
| Testes pytest | 28 passando (antes: 21) |
| Os 7 testes novos contra o código anterior | 7 falhando, como esperado |
| Lint (ruff) | limpo |
| CNAEs reais (`F.K03200$Z.D40810.CNAECSV`) | 1.359/1.359 lidos (antes: 145) |
| Acentuação em dado real | `Produção de sementes certificadas` íntegro |
| `;` dentro de campo | `LOJA A; LOJA B` preservado, sem coluna extra |
| Ingestão end-to-end com `--uf MG` | 289 estabelecimentos (antes: 0) |

> As contagens de CNAE foram medidas sobre o arquivo real já baixado em
> `data/raw/2024-08/`. A carga real completa (todas as 10 fatias) segue
> pendente de uma janela em que a infraestrutura da RFB coopere.

---

## [2.1.1] — 2026-09-08

Primeira execução com **dados reais** da Receita. O download das 22 fatias
funcionou (confirmando a correção 2.1.0); a carga quebrou no parsing.

### Corrigido — CRÍTICO

- **Arquivos de domínio abortavam a carga inteira.** O parser CSV falhava com
  `Error when sniffing file ... CNAECSV` / *"columns are set as 2, sniffer
  found 3"*.
  **Causa raiz:** os arquivos da Receita são inconsistentes — parte das linhas
  vem sem aspas, e algumas descrições contêm `;`. Uma linha como

  ```
  4618401;Representantes comerciais; agentes do comercio
  ```

  faz o parser enxergar 3 campos e abortar a leitura do arquivo inteiro.
  → Passamos a ler a **linha inteira como campo único** (delimitador `\x07`,
  ausente no arquivo) e a partir no **primeiro `;`**. Como o código nunca
  contém `;`, tudo à direita é a descrição — não importa quantos `;` ela tenha
  nem se está entre aspas. **Zero linhas perdidas.**
  Descartamos `ignore_errors=true`: descartaria a linha em silêncio, e perder
  um CNAE significa empresas deixando de casar com o ICP.

### Corrigido — ALTO

- **Arquivos largos sem tolerância a linha malformada.** `Estabelecimentos`
  (30 colunas) tem a mesma classe de defeito, e uma única linha ruim derrubaria
  a fatia inteira.
  → `ignore_errors=true` + **`store_rejects=true`**: linhas ruins não derrubam
  a carga, mas também **não somem em silêncio** — são contabilizadas e
  exibidas. Acima de **1%** de rejeição a ingestão falha com mensagem
  apontando o dicionário de layout, porque aí o problema não é "linha ruim" e
  sim "layout mudou".

- **Competência assumida pelo calendário.** `run_pipeline.py` usava o mês
  corrente como competência atual. Como a Receita publica com semanas de
  defasagem, em 08/09 o pipeline tentava processar `2026-09`, ainda não
  publicada.
  → O orquestrador agora **descobre** a última competência publicada e valida
  que atual e anterior existem no share antes de começar, listando as
  disponíveis quando não existem.

### Adicionado

- Fixtures sintéticas passam a reproduzir os **defeitos reais** da Receita:
  descrição com `;`, aspas internas e 1 em cada 3 linhas sem aspas. O CI agora
  detectaria essa regressão sozinho.
- 2 testes de regressão (parsing sujo sem perda de linha; limiar de rejeição).
- Relatório de linhas rejeitadas por tabela na saída da ingestão.

### Verificação

| Verificação | Resultado |
|---|---|
| Testes pytest | 21 passando (antes: 19) |
| Testes dbt | 34 passando |
| Lint (ruff) | limpo |
| CNAEs patológicos | 12/12 lidos, `;` preservado na descrição |
| Pipeline end-to-end | OK |

> Nota: numa descrição com aspa não escapada (`peças 1" e 2"`), a aspa final é
> removida — ambiguidade insolúvel sem conhecer o escape da RFB. A linha é
> preservada, que é o que importa.

---

## [2.1.0] — 2026-09-08

Correção de fonte de dados. Todos os endereços do projeto foram revisados
contra o que está efetivamente no ar.

### Corrigido — CRÍTICO

- **Host de download desativado.** O projeto apontava para
  `dadosabertos.rfb.gov.br/CNPJ/dados_abertos_cnpj/AAAA-MM/` e variações em
  `arquivos.receitafederal.gov.br/.../dados_abertos_cnpj/`. **Ao final de
  janeiro/2026 a Receita Federal migrou a publicação** para um
  compartilhamento WebDAV (Nextcloud), e os caminhos antigos deixaram de
  existir. Isso explica por que nenhum espelho respondia e por que a
  ingestão de `Estabelecimentos` nunca concluiu.
  → Novo módulo `src/receita.py` conversa com o share atual:
  - listagem via `PROPFIND` em
    `arquivos.receitafederal.gov.br/public.php/webdav`;
  - download em
    `arquivos.receitafederal.gov.br/public.php/dav/files/<token>/<AAAA-MM>/<arquivo>`.

- **Adivinhação de competência substituída por descoberta.** A versão anterior
  varria até 12 meses testando URLs para achar qual existia — desperdício de
  requisições e fonte de falsos negativos.
  → O pipeline agora **lista** o que a Receita publicou. `--competencia`
  passou a ser opcional (default: a mais recente publicada), e
  `python -m src.ingestao --listar` mostra competências e arquivos
  disponíveis.

- **Mirror comunitário removido.** A versão anterior caía, como último
  recurso, num mirror do GitHub congelado em `2024.09`. Um pipeline de
  detecção de eventos que silenciosamente usa dados de dois anos atrás produz
  um digest inteiro de "empresas novas" falsas.
  → Removido. Falha explícita é melhor que dado velho disfarçado de atual.

### Alterado

- **Share token configurável.** `RADAR_RFB_SHARE_TOKEN` permite trocar o token
  sem editar código, caso a Receita publique um novo compartilhamento. A
  mensagem de erro ensina como obtê-lo.
- **Diagnóstico acionável.** Falhas de listagem distinguem "host fora do ar"
  de "token inválido/mudou", com instruções específicas em cada caso.
- **README** ganhou tabela de endereços oficiais, nota sobre a migração de
  janeiro/2026 e o procedimento de troca de token.
- `src/config.py` centraliza os endereços, incluindo o catálogo no
  [dados.gov.br](https://dados.gov.br/dados/conjuntos-dados/cadastro-nacional-da-pessoa-juridica---cnpj)
  e o [dicionário de layout](https://www.gov.br/receitafederal/dados/cnpj-metadados.pdf).

### Adicionado

- 5 testes de regressão: extração de competências e de arquivos do XML
  WebDAV, exclusão de não-ZIP na listagem, formato da URL de download e —
  via AST — a garantia de que nenhum módulo volte a **apontar** para os hosts
  desativados (comentários e docstrings podem citá-los para documentar a
  migração).

### Verificação

| Verificação | Resultado |
|---|---|
| Lint (ruff) | limpo |
| Testes pytest | 19 passando (antes: 14) |
| Testes dbt | 34 passando |
| Pipeline end-to-end | OK |
| Parser WebDAV | validado contra XML no formato Nextcloud |

> O download com dados reais ainda depende da disponibilidade da
> infraestrutura da RFB, historicamente instável. Os endereços agora estão
> corretos; a primeira carga real segue pendente.

---

## [2.0.0] — 2026-09-07

Refatoração estrutural após auditoria completa do código. O objetivo foi
fechar a distância entre o que o Plano Diretor V2 **declara** e o que o
repositório **implementava**: vários princípios inegociáveis (idempotência,
"a IA nunca inventa", ICP desacoplado, score explicável com fatores) estavam
escritos no documento mas ausentes do código.

Estado anterior: o pipeline nunca havia executado com dados reais. O banco
versionado continha 3 estabelecimentos fictícios (`11111111`, `22222222`,
`33333333`), e o digest gerado era integralmente sintético.

### Corrigido — CRÍTICO

- **Conteúdo fabricado apresentado como análise de IA.**
  `gerar_mock()` devolvia texto inventado ("Escritório focado em assessoria
  contábil e fiscal") com `confianca_ia: "Alta"` fixo, renderizado pelo digest
  sob o título "Inteligência Comercial (Contexto da IA)", indistinguível de
  saída real. Violava os princípios 6 e 7 do Plano Diretor e representava
  risco reputacional direto na validação com clientes.
  → Agora toda linha enriquecida carrega `origem` (`llm` | `heuristica`) e
  `modelo`. O digest estampa a procedência em cada oportunidade e exibe um
  aviso no topo quando há itens sem interpretação de IA. O fallback deixou de
  imitar análise: declara "não inferido" nos campos que dependem do modelo.

- **Falhas de IA mascaradas silenciosamente.**
  Um `except Exception` genérico capturava qualquer erro (chave inválida,
  rate limit, modelo aposentado, timeout) e o substituía por texto inventado,
  sem contagem nem alerta.
  → Falhas passam a ser contadas, registradas e reportadas. Acima de 30% de
  taxa de falha, a execução é interrompida com mensagem acionável, em vez de
  produzir um digest degradado.

- **Ausência de diff entre competências — o Event Store não existia de fato.**
  A ingestão usava `CREATE OR REPLACE TABLE`, sem coluna de competência nem
  particionamento: existia uma única foto, sobrescrita a cada execução.
  `evt_new_company` detectava empresas novas por
  `data_inicio_atividade >= current_date - 30`, o atalho que a cartilha havia
  descartado. Consequências: resultado variava conforme o dia da execução
  (não idempotente); empresas incluídas com data retroativa eram perdidas; e
  não havia fundação para nenhum outro evento do catálogo.
  → Competência passa a ser cidadã de primeira classe. Bronze grava
  `data/bronze/<tabela>/competencia=AAAA-MM/*.parquet`. `evt_new_company` usa
  **set difference** real entre a competência atual e a anterior — a mesma
  mecânica que sustentará `STATUS_CHANGED`, `PARTNER_*` e demais eventos.

- **Ingestão incompleta.** Baixava apenas `Estabelecimentos0.zip` (1 de 10
  fatias, ~10% do país) — insuficiente para um ICP municipal, podendo zerar
  o digest.
  → Baixa as 10 fatias de `Estabelecimentos` e `Empresas`, com filtro
  opcional por UF para desenvolvimento (`--uf MG`).

### Corrigido — ALTO

- **Filtro de ICP inócuo (`ilike '%ti%'`).** O padrão casava com
  "A-**TI**-vidades", presente em boa parte dos CNAEs brasileiros, além de
  "cosmé**ti**cos" e "Par**ti**cipação". Na prática, o filtro de segmento não
  filtrava: o ICP era "qualquer empresa em Varginha".
  → Casamento passa a ser por **código CNAE**, com listas de primários,
  secundários e **excluídos** (um escritório de contabilidade não deve
  prospectar outro escritório de contabilidade). Teste de regressão em
  `tests/test_pipeline.py` documenta o bug.

- **Tabela `empresas` ausente — digest sem nome de empresa.** O produto
  identificava a oportunidade apenas por `CNPJ 11111111000199`, inacionável
  para o usuário final. Também impedia os fatores de score `porte` e
  `capital`.
  → `slv_empresas` adicionado, trazendo razão social, capital social e porte.
  `nome_fantasia` (coluna 04), antes descartado, também passa a ser ingerido.

- **Score com apenas 3 valores possíveis.** A fórmula era base fixa 50 +
  recência (35/15/0), resultando em 50, 65 ou 85 — sem discriminar dentro da
  mesma faixa. A "base ICP +50" era constante para todos os aprovados, logo
  não pontuava nada. `where opportunity_score >= 50` era logicamente inerte.
  → Seis fatores ponderados (recência com decaimento linear, aderência de
  CNAE, porte, localização, capital social, completude de contato), com pesos
  vindos do ICP. Distribuição verificada: 16 valores distintos no conjunto de
  teste.

- **Score não auditável.** A explicação era uma string concatenada, sem os
  valores por trás.
  → `score_fatores` persistido como JSON estruturado; o texto exibido é
  derivado dele. Teste de dados garante que a soma dos fatores é exatamente
  igual ao score exibido.

- **ICP hardcoded em SQL.** `municipio_nome = 'VARGINHA'` cravado no model —
  cada cliente novo exigiria editar SQL.
  → ICP em `config/icp/*.yaml`, carregado para tabelas de configuração por
  `scripts/carregar_icp.py`. Novo cliente = novo arquivo YAML.

- **Projeto não reproduzível.** Não havia `requirements.txt`, `pyproject.toml`
  nem `environment.yml` — impossível clonar e executar.
  → `pyproject.toml` com dependências, extras (`ia`, `dev`) e configuração de
  `ruff`/`pytest`.

- **`.gitignore.txt`** — a extensão `.txt` fazia o Git ignorar o próprio
  arquivo de ignore. → Renomeado para `.gitignore`.

- **CI quebrado por construção.** O workflow era o template padrão do GitHub:
  usava conda, referenciava um `environment.yml` inexistente e rodava
  `pytest` sem testes — falhava em todo push.
  → Substituído por `ci.yml` (lint + testes + **pipeline end-to-end com
  fixtures sintéticas**) e `pipeline-mensal.yml` (execução agendada).

### Corrigido — MÉDIO

- **`verify=False` em todos os downloads**, desabilitando verificação TLS.
  → Verificação ativada por padrão; desligamento apenas explícito via
  `RADAR_TLS_INSECURE=1`.

- **SDK e modelo defasados.** `google.generativeai` (legado) com
  `gemini-1.5-flash` (aposentado) — a chamada real provavelmente já falhava,
  e falhava silenciosamente virando texto fabricado.
  → Migrado para `google-genai`, modelo configurável via `RADAR_LLM_MODEL`
  (default `gemini-2.0-flash`).

- **Enriquecimento sem cache.** Cada execução fazia `DROP TABLE` e
  reprocessava tudo, re-gastando tokens — contrariando "pré-computado, nunca
  re-pagar pela mesma empresa".
  → Tabela persistente com `event_id` como chave; apenas oportunidades
  inéditas são enviadas ao modelo.

- **Prompt sem grounding.** Não proibia invenção, não recebia o ICP nem o
  score/fatores, e pedia `dor_provavel` como se fosse fato apurado.
  → `system_instruction` com regras explícitas; ICP, score e fatores passam a
  compor o contexto; o campo foi renomeado para `hipotese_de_dor` e é
  apresentado como hipótese setorial, não como fato sobre a empresa.

- **Schemas inconsistentes.** O dbt gravava tudo em `main` (o `profiles.yml`
  não definia schema) enquanto o script Python gravava em `gold`; as pastas
  `silver/`/`gold/` eram puramente cosméticas.
  → Schemas configurados por camada em `dbt_project.yml`.

- **Zero testes de dados.** `tests/` vazio, nenhum `schema.yml`.
  → 24 testes dbt (`unique`, `not_null`, `accepted_values`) mais 5 testes
  singulares, incluindo o teste central do Event Store: nenhum evento
  `NEW_COMPANY` pode referenciar CNPJ já existente na competência anterior.

- **Ausência de observabilidade** (§16 do Plano Diretor).
  → `src/observabilidade.py` registra em `meta.run_log` etapa, competência,
  status, contagens, chamadas ao LLM, duração e falhas.

- **Ausência de ciclo de feedback** (§13).
  → Tabela `gold.opportunity_feedback` e campos de marcação em cada
  oportunidade do digest.

- **`extrair_csv` assumia um único arquivo por ZIP** (`namelist()[0]`).
  → Extrai todos os membros.

- **Download sem verificação de integridade.** Bastava o arquivo existir; um
  ZIP truncado passava como válido.
  → `_zip_integro()` valida via `testzip()`; arquivos corrompidos são
  rebaixados. Coberto por testes.

- **Orquestrador frágil.** Usava `python` literal e `shell=True`, quebrando
  fora do venv ativo; não recebia competência; não rodava testes.
  → `sys.executable` com lista de argumentos; competência parametrizada e
  propagada ao dbt via `--vars`; usa `dbt build` (models + testes), de modo
  que dado ruim interrompe o pipeline antes de virar digest.

- **README desatualizado**, descrevendo a V1 e um roadmap terminando em BI.
  → Reescrito para a V2.

### Adicionado

- `scripts/gerar_fixtures.py` — gera dados sintéticos no formato exato da
  Receita (30 colunas, `latin-1`, `;`), em duas competências, permitindo
  desenvolver e testar o pipeline sem depender do download de ~85 GB nem da
  instabilidade dos espelhos oficiais. Usado também pelo CI.
- `src/config.py` — layout oficial e caminhos centralizados. Uma mudança de
  layout da Receita passa a ser alteração de uma linha, não uma caçada por
  índices espalhados no SQL.
- `src/observabilidade.py` — run log estruturado.
- `dbt_radar/models/bronze/` — camada Bronze explícita lendo os Parquet
  particionados via `hive_partitioning`.
- `slv_dominios` — CNAEs e municípios deduplicados pela competência mais
  recente.
- Priorização por faixas (alta/média/baixa) no digest, conforme §12 do Plano
  Diretor.
- Campo `confidence` no Event Store: distingue abertura recente de inclusão
  retroativa/correção cadastral.

### Verificação

Pipeline executado de ponta a ponta com duas competências sintéticas
(400 e 460 estabelecimentos, 60 empresas exclusivas da competência mais
recente):

| Verificação | Resultado |
|---|---|
| Set difference | 58 eventos detectados (60 novas − 2 inativas, corretamente filtradas por `situacao_cadastral`) |
| Testes dbt | 34 passando, 0 erros |
| Testes pytest | 14 passando |
| Lint (ruff) | limpo |
| Idempotência | duas execuções completas → `event_id` idênticos |
| Cache de enriquecimento | segunda execução: 0 chamadas ao LLM |
| Distribuição de score | 16 valores distintos (antes: 3) |

### Pendências conhecidas

- O pipeline **ainda não foi executado com dados reais da Receita** — os
  espelhos oficiais não responderam durante a refatoração. A ingestão está
  pronta e parametrizada; falta a primeira carga real.
- Catálogo de eventos permanece com `NEW_COMPANY` apenas, por decisão de
  escopo. A fundação (competência + Parquet particionado) já suporta os
  demais.
- Snapshots SCD-2 não implementados: só serão necessários para eventos de
  mudança de atributo.
- Pesos do score são hipóteses iniciais, a calibrar com dados reais de
  conversão.
- Envio de e-mail permanece manual (concierge), conforme o MVP.

---

## [1.0.0] — 2026-08-25

- Versão inicial: ingestão de `Estabelecimentos0`, models dbt
  (`slv_estabelecimentos`, `evt_new_company`, `gld_icp_matches`,
  `gld_opportunities`), enriquecimento via Gemini e geração de digest em
  Markdown. Encadeamento ponta a ponta funcionando com dados de exemplo.
