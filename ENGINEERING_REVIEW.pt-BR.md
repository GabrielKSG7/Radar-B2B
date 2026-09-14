[English](ENGINEERING_REVIEW.md) · **Português**

# Revisão do Radar B2B contra o Plano Diretor V2

Data: 14/09/2026 · Referência: `Plano_Diretor_Radar_B2B_V2.pdf` e
`Cartilha_Radar_B2B_V2.pdf`

Auditoria de aderência do código ao Plano Diretor, feita lendo os dois
documentos por inteiro e todos os módulos do projeto. O objetivo é chegar à
**validação empírica** (§14 do PD) sabendo o que está pronto, o que está
quebrado e o que é ruído.

---

## 1. Veredito

A arquitetura está **fiel ao Plano Diretor** — em vários pontos, literalmente.
Os pesos do Opportunity Score no `contabilidade_sul_mg.yaml`
(recência 30, cnae 25, porte 15, localização 10, capital 10, contexto 10)
reproduzem exatamente a tabela do §10. O contrato Pydantic do enriquecimento
implementa o §11 com rigor, incluindo a proibição explícita de inventar
faturamento, sócios ou contatos. O `meta.run_log` cobre o §16. A decisão de
não ingerir a tabela de sócios cumpre o §17.

O que separa o projeto do uso real **não é arquitetura, é dado**: a Fase 4 do
roadmap ("NEW_COMPANY — empresas novas detectadas sem duplicidade") ainda não
tem seu *Definition of Done* cumprido.

Há também um defeito grave descoberto nesta revisão: **o `run_pipeline.py` não
aceita mais argumentos de linha de comando**, embora o README e os dois
workflows do GitHub Actions dependam disso.

---

## 2. Aderência por fase do roadmap (§15)

| Fase | Definition of Done | Situação |
|---|---|---|
| 0 · Diagnóstico | Estado e prioridades definidos | ✅ |
| 1 · Setup | Pipeline executa localmente | ✅ |
| 2 · Bronze | Competência baixada, validada e persistida | ⚠️ validação OK; carga completa pendente |
| 3 · Silver | Tabelas tipadas e testes passando | ✅ 24 testes dbt |
| 4 · NEW_COMPANY | Empresas novas **sem duplicidade** | ❌ **bloqueio atual** |
| 5 · ICP | Eventos filtrados por ICP | ✅ |
| 6 · Score | Score e fatores por oportunidade | ✅ |
| 7 · LLM | JSON estruturado + confiança + grounding | ⚠️ falta registrar campos do grounding |
| 8 · Digest | Usuário recebe e entende | ✅ |
| 9 · Validação | Feedback e sinais de ação coletados | ⛔ não iniciada (depende da Fase 4) |
| 10 · Automação | Pipeline agenda e registra execução | ❌ workflow existe mas **não funciona** |
| 11 · Expansão | Só eventos com valor comprovado | ⛔ corretamente adiada |
| 12 · Platform | Após validação comercial | ⛔ corretamente adiada |

---

## 3. Achados

### 3.1 CRÍTICO — o orquestrador perdeu a interface de linha de comando

> **RESOLVIDO na 2.2.0.** Registro mantido porque o achado explica
> por que o CI passou meses sem exercitar as fixtures.

O `run_pipeline.py` hoje fixa competências, fatias e UF em constantes e **não
lê `sys.argv`**. Mas:

- o **README** documenta
  `python run_pipeline.py --competencia 2026-08 --anterior 2026-07 --somente-local --sem-ia`;
- o **`ci.yml`** chama exatamente essa linha;
- o **`pipeline-mensal.yml`** chama `--uf MG --competencia <mês>`.

Python não reclama de argumentos que ninguém lê: eles são **silenciosamente
ignorados**. Verificado executando o `main()` com exatamente a linha do
`ci.yml` e interceptando os comandos:

```
entrada:  --competencia 2026-08 --anterior 2026-07 --somente-local --sem-ia

executaria de fato:
  python -m src.ingestao --competencia 2024-07 --fatias 0,1,...,9 --uf MG
  python -m src.ingestao --competencia 2024-08 --fatias 0,1,...,9 --uf MG
  ...
  honrou --somente-local?  False
  honrou --sem-ia?         False
```

As consequências:

- **O CI não testa o que diz testar — e é pior que isso.** Ele gera fixtures,
  manda rodar com `--somente-local`, e o pipeline sai baixando as 10 fatias
  reais da Receita. Ou seja: **todo push para `main` dispara uma tentativa de
  download de dezenas de GB** de um host historicamente instável. O job falha
  por tempo ou rede, nunca pelo motivo que alguém suporia, e as fixtures — que
  são a única proteção real contra regressão de parsing — jamais são
  exercitadas.
- **A automação mensal está inerte.** O `--competencia` do
  `workflow_dispatch` é descartado; todo dia 5 o pipeline reprocessaria
  2024-08 para sempre, independentemente do que a Receita publicou.

Assumo a parte que me cabe: ao transformar as competências em constantes (2.1.6)
eu tornei o acoplamento estrutural sem restaurar a CLI de que README e CI
dependem. A correção certa é `argparse` com *default* nas constantes atuais —
mantém o comportamento de hoje e devolve a interface documentada.

### 3.2 CRÍTICO — Fase 4 sem DoD: as fatias não são estáveis

Já documentado no CHANGELOG 2.1.7. Medição: com a mesma fatia 0 nas duas
competências, **69,2% dos CNPJs de julho não existem em agosto**. O mesmo CNPJ
muda de arquivo a cada publicação, então o *set difference* entre cargas
parciais mede sorteio, não abertura de empresa.

Não há atalho: a detecção exige as 10 fatias nas duas competências. A ingestão
fatia a fatia (2.1.7) tornou isso viável em disco; falta executar.

### 3.3 ALTO — chaves do ICP silenciosamente ignoradas

> **RESOLVIDO na 2.2.0.**

O `contabilidade_sul_mg.yaml` declara:

```yaml
geografia:
  uf: [MG]          # <- nunca lido
  municipios: [...]  # <- lido
eventos: [NEW_COMPANY]   # <- nunca lido
```

O `scripts/carregar_icp.py` lê apenas `geografia.municipios`, `cnae`, `porte`
e `pesos`. Quem configurar um ICP novo vai supor que `uf` e `eventos`
funcionam — o PD §9 lista os dois como atributos do ICP. Hoje, restringir por
UF no YAML não tem efeito algum, e o dia em que existir um segundo tipo de
evento, `eventos:` continuará decorativo.

Mesma família dos bugs desta sessão: configuração que parece ativa e não é.

### 3.4 MÉDIO — observabilidade com buracos (§16)

O `meta.run_log` é bom, mas só três etapas se registram nele: ingestão,
enriquecimento e digest. **O dbt e a carga do ICP não aparecem**, porque o
orquestrador os executa como subprocesso sem envolvê-los em `obs.etapa`.

O §16 pede explicitamente registrar:

- *"quantidade de eventos detectados e oportunidades geradas"* — hoje o
  `run_log` guarda `registros` da ingestão, não a contagem de eventos nem de
  oportunidades;
- *"quantidade **e custo** das chamadas ao LLM"* — `chamadas_llm` existe,
  custo não.

### 3.5 MÉDIO — grounding sem rastro dos campos usados (§11)

O §11 pede que a inferência tenha *"referência aos campos usados no grounding,
quando isso for tecnicamente viável"*. O `ContextoComercial` tem `confianca`,
mas não registra de quais campos a interpretação saiu. Como o prompt é montado
a partir de um conjunto fixo e conhecido de colunas, isso é perfeitamente
viável — e é o que permite auditar uma explicação meses depois.

### 3.6 OTIMIZAÇÃO — `empresas` ingerida para o país inteiro

> **RESOLVIDO na 2.2.0.**

`Estabelecimentos` é filtrada por UF; `empresas` não, porque a tabela não tem
coluna de UF. Com as 10 fatias, o Bronze guardará **~180 milhões de linhas por
competência** para servir a ~2 milhões de estabelecimentos de MG.

Como `empresas` só é usada em join por `cnpj_basico` com os estabelecimentos
já filtrados, dá para restringi-la aos `cnpj_basico` que sobreviveram ao filtro
de UF — redução de duas ordens de grandeza em disco e no tempo do
`slv_empresas`. O `--uf` já é assumidamente um recurso de velocidade de
desenvolvimento, então a mudança é coerente com o que existe.

### 3.7 BAIXO — `python-package-conda.yml` órfão

O `ci.yml` foi criado para substituir o template padrão do GitHub, mas o
template continua no repositório e roda a cada push, falhando. Ruído no
histórico de CI.

---

## 4. O que está certo e não deve ser mexido

Registro explícito, porque em revisão só se fala do que está errado:

- **Score determinístico e auditável** (§10): seis fatores, pesos vindos do
  ICP, `score_fatores` em JSON e explicação derivada dos fatores — não
  concatenada à mão. Atende ao pedido de transparência do PD.
- **ICP como configuração** (§9): adicionar cliente é criar YAML, não editar
  SQL.
- **Fronteira da IA** (§11): o LLM não decide fato nem score, a saída é
  validada com Pydantic, a procedência (`llm` | `heuristica`) é persistida e o
  digest se marca como não enriquecido quando a IA falta. É exatamente a
  disciplina que o PD exige.
- **Ciclo de feedback** (§13): `gold.opportunity_feedback` já registra o que
  foi enviado, pronto para receber o retorno do escritório.
- **LGPD** (§17): sócios fora do MVP, competência registrada como origem,
  contato mantido apenas do estabelecimento.
- **Fixtures que reproduzem os defeitos da fonte**: acento, `;` dentro de
  campo, aspas curvas cp1252, linhas sem aspas. Isso é o que transforma o CI em
  proteção real — desde que ele volte a rodar (3.1).

---

## 5. Prioridades sugeridas

| # | Item | Por quê agora |
|---|---|---|
| ~~1~~ | ~~Restaurar CLI do `run_pipeline.py`~~ (3.1) | ✅ feito na 2.2.0 |
| 2 | Carga completa das 10 fatias (3.2) | É o DoD da Fase 4 e o portão para tudo que vem depois |
| ~~3~~ | ~~Ler `geografia.uf` e `eventos` do ICP~~ (3.3) | ✅ feito na 2.2.0 |
| 4 | Eventos/oportunidades e custo de LLM no `run_log` (3.4) | §16; e sem isso não dá para medir a validação |
| ~~5~~ | ~~Filtrar `empresas` por UF~~ (3.6) | ✅ feito na 2.2.0 |
| 6 | Campos de grounding (3.5) | §11; melhora auditabilidade, não bloqueia validação |
| 7 | Remover workflow órfão (3.7) | Higiene |

Os itens 1, 3 e 5 são pequenos e deveriam vir **antes** da carga completa: o 5
muda o que a carga grava, e refazer 180 milhões de linhas depois seria
desperdício.

---

## 6. Passo-a-passo até o uso empírico

Cada etapa tem **critério de aceite verificável**. Se o critério não bater, o
problema é ali — não adiante para a etapa seguinte carregando dúvida.

### ~~Etapa 1 · Destravar a interface~~ ✅ concluída na 2.2.0

Três correções pequenas que precisam vir antes da carga completa, porque duas
delas mudam o que a carga grava:

1. **CLI do `run_pipeline.py`** (achado 3.1): `argparse` com `--competencia`,
   `--anterior`, `--uf`, `--fatias`, `--somente-local`, `--sem-ia`, `--icp`,
   usando as constantes atuais como *default*. Comportamento sem argumentos
   fica idêntico ao de hoje.
2. **ICP honrando `geografia.uf` e `eventos`** (3.3).
3. **`empresas` filtrada pelos `cnpj_basico` de MG** (3.6) — precisa ser antes,
   senão a carga grava 180 milhões de linhas que serão descartadas depois.

*Aceite:* `pytest -q` verde; `python run_pipeline.py --somente-local --sem-ia
--competencia 2026-08 --anterior 2026-07` roda com fixtures sem tocar a rede;
CI verde no push.

### Etapa 2 · Carga completa (horas, tolerante a interrupção)

```powershell
python run_pipeline.py
```

As 10 fatias das duas competências. O `[SKIP]` reaproveita o que já está no
disco, então quedas são retomáveis — rode de novo quantas vezes precisar.
Prefira horário de baixo movimento; a infraestrutura da RFB já nos custou caro
nesta sessão.

*Aceite:* as duas competências com 10 parquet cada em
`data/bronze/estabelecimentos/competencia=*/`, e o `dbt build` fechando
`PASS=34 ERROR=0`.

### Etapa 3 · O portão de qualidade

```powershell
python scripts/diagnostico_eventos.py
```

Este é o momento que decide se o produto existe. Números esperados agora que a
população é a mesma nos dois meses:

| Indicador | Esperado | Se vier diferente |
|---|---|---|
| `[2.1]` sumiços | perto de 0% | populações ainda divergem; não avance |
| eventos detectados | ordem de milhares, não centenas de milhares | filtro de situação cadastral ou competência errada |
| datas dentro da janela | maioria | ainda há artefato de ingestão |
| veredito | "eventos coerentes" | investigue antes de olhar o digest |

*Aceite:* veredito positivo. **Sem isso, nada do que vem depois significa
alguma coisa.**

### Etapa 4 · Ler o digest como produto, não como saída de script

Aqui muda a natureza do trabalho: até agora era engenharia, daqui em diante é
produto. Abra `data/digests/` e leia como se fosse o contador em Varginha:

- O volume é utilizável? O PD §14 pede **10 a 30 oportunidades** — se vierem
  300, suba `score_minimo`; se vierem 3, o ICP está estreito demais.
- A explicação de "por que importa" convence, ou é genérica?
- A ação sugerida é concreta o bastante para alguém executar hoje?
- Os CNAEs do ICP são mesmo os clientes que um escritório de contabilidade
  quer? A lista atual (restaurantes, vestuário, construção, médica,
  transporte) é hipótese sua, não dado — e é a primeira coisa que o
  escritório real vai contestar.

*Aceite:* você olharia para essa lista e saberia quem contatar primeiro.

### Etapa 5 · Enriquecimento com IA

Só agora vale gastar cota: interpretar semanticamente oportunidades derivadas
de dados ruins é caro e inútil. Gere uma chave nova (a anterior foi exposta) e
configure por variável de ambiente:

```powershell
setx GEMINI_API_KEY "a-nova-chave"
```

*Aceite:* digest sem a marcação "NÃO ENRIQUECIDO", e as explicações passando no
teste do §11 — nenhuma cita faturamento, funcionários, sócios ou contatos que
não estejam nos dados.

### Etapa 6 · Concierge (§14) — o uso empírico de fato

O PD é específico e vale seguir à risca:

1. Um ou dois escritórios em Varginha. Dois é melhor que um: divergência entre
   eles ensina mais que concordância de um só.
2. ICP por escritório — arquivo YAML separado, o que o desenho já suporta.
3. Entregue o digest **manualmente**. Nada de automatizar envio agora; o
   valor da fase está em assistir à reação.
4. A pergunta certa, segundo o próprio PD §1.4, **não** é *"você quer esse
   sinal?"* — o mercado já provou que sim. É: *"o que isto te dá que a Mira
   não dá, e você pagaria por isso?"*
5. Registre o retorno em `gold.opportunity_feedback`, que já está pronto para
   recebê-lo.

*Aceite:* as métricas do §13 saindo do zero — % considerado útil, % contatado,
respostas.

### O que NÃO fazer agora

O PD tem uma regra de avanço explícita: *"a próxima camada tecnológica só deve
ser priorizada quando houver evidência de valor na camada anterior."* Portanto,
até a Etapa 6 dar sinal:

- nada de novos eventos (`STATUS_CHANGED`, `PARTNER_*`);
- nada de dashboard ou API;
- nada de automação de envio.

A automação mensal (Fase 10) é a exceção defensável — o workflow já existe e só
precisa da Etapa 1 para funcionar.
