import argparse
import subprocess
import sys
import time


def run_step(step_name, command, cwd=None):
    """Executa um comando no terminal e para o pipeline se houver erro."""
    print(f"\n{'='*50}")
    print(f"🚀 INICIANDO ETAPA: {step_name}")
    print(f"{'='*50}")

    start_time = time.time()

    try:
        subprocess.run(
            command,
            shell=True,
            check=True,
            cwd=cwd,
            text=True
        )
        elapsed_time = time.time() - start_time
        print(f"✅ [SUCESSO] {step_name} concluída em {elapsed_time:.2f} segundos.")
    except subprocess.CalledProcessError:
        print(f"❌ [FALHA CRÍTICA] Erro na etapa: {step_name}.")
        print("Interrompendo o pipeline para evitar propagação de erros.")
        sys.exit(1)

# --------------------------------------------------------------------------
# Parâmetros do teste offline — FONTE ÚNICA DE VERDADE.
#
# Estes valores alimentam a ingestão E o dbt. Quando estavam separados, a
# ingestão carregava 2024-07/2024-08 e o dbt transformava 2026-07/2026-08
# (o default do dbt_project.yml, porque o `dbt run` ia sem --vars): dez
# modelos "OK", zero eventos, digest vazio e nenhum erro em lugar nenhum.
#
# FATIAS: use SEMPRE as dez para qualquer resultado que alguém vá ler.
#
# Medido em 2024-07 vs 2024-08 com a mesma fatia 0 nas duas competências:
# 69% dos CNPJs de julho não aparecem em agosto, e só ~30% são comuns. Ou
# seja, a fatia NÃO é estável entre publicações — o mesmo CNPJ muda de arquivo
# a cada mês. Duas competências ingeridas com "a fatia 0" são dois recortes
# diferentes do mesmo universo, e o set difference entre elas mede sorteio,
# não abertura de empresa. Nem o
# assert_evento_nao_existe_na_competencia_anterior detecta isso: sob o
# critério dele as empresas faltantes são legitimamente ausentes.
#
# "0" continua válido para testar o encanamento — a ingestão avisa em voz
# alta que a carga é parcial e que o digest não vale.
# --------------------------------------------------------------------------
COMPETENCIA_ANTERIOR = "2024-07"
COMPETENCIA_ATUAL = "2024-08"
FATIAS = "0,1,2,3,4,5,6,7,8,9"
UF = "MG"
ICP = "contabilidade_sul_mg"


def parse_args(argv=None):
    """CLI do orquestrador — os defaults são as constantes acima.

    Sem esta função o script ignorava `sys.argv` em silêncio, e como Python
    não reclama de argumento que ninguém lê, README, ci.yml e
    pipeline-mensal.yml passavam opções que nunca surtiam efeito: o CI pedia
    `--somente-local` e o pipeline saía baixando dezenas de GB da Receita a
    cada push. Ver REVISAO_PD_V2.md §3.1.
    """
    ap = argparse.ArgumentParser(
        description="Orquestrador do pipeline Radar B2B")
    ap.add_argument("--competencia", default=COMPETENCIA_ATUAL,
                    help=f"competência atual AAAA-MM (default: {COMPETENCIA_ATUAL})")
    ap.add_argument("--anterior", default=COMPETENCIA_ANTERIOR,
                    help=f"competência comparada (default: {COMPETENCIA_ANTERIOR})")
    ap.add_argument("--uf", default=UF,
                    help=f"filtra estabelecimentos por UF (default: {UF})")
    ap.add_argument("--fatias", default=FATIAS,
                    help="fatias a ingerir, ex: 0,1 (default: todas)")
    ap.add_argument("--icp", default=ICP,
                    help=f"ICP ativo (default: {ICP})")
    ap.add_argument("--somente-local", action="store_true",
                    help="não baixa; usa os ZIPs já presentes em data/raw")
    ap.add_argument("--sem-ia", action="store_true",
                    help="pula o LLM; enriquecimento só por heurística")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    print("🌟 INICIANDO PIPELINE RADAR B2B 🌟")
    print(f"   {args.anterior} -> {args.competencia} | fatias {args.fatias} | "
          f"UF {args.uf} | ICP {args.icp}"
          + ("  [somente local]" if args.somente_local else "")
          + ("  [sem IA]" if args.sem_ia else "") + "\n")
    total_start = time.time()

    # 1. Ingestão Bronze (O script fará o SKIP automático do que já existir no disco)
    somente_local = " --somente-local" if args.somente_local else ""
    for rotulo, competencia in (("1A", args.anterior), ("1B", args.competencia)):
        run_step(
            f"{rotulo}. INGESTÃO BRONZE ({competencia})",
            f"python -m src.ingestao --competencia {competencia} "
            f"--fatias {args.fatias} --uf {args.uf}{somente_local}",
        )

    # 2. Configuração: ICP do YAML para as tabelas config.* do DuckDB.
    #
    # NÃO REMOVER. O dbt lê config.icp em gld_icp_matches; sem este passo ele
    # falha com 'schema "config" does not exist'. Antes o pipeline funcionava
    # por acidente, aproveitando a tabela deixada no radar.duckdb por uma
    # execução manual anterior — ou seja, dependia de estado residual e
    # quebrava em qualquer máquina limpa. Rodar sempre custa ~1s, é idempotente
    # (CREATE OR REPLACE) e mantém o ICP em sincronia com o YAML.
    run_step("2. CARGA DO ICP (config.icp)", "python scripts/carregar_icp.py")

    # 3. Transformação e Motor de Eventos (CDC)
    #
    # --vars é obrigatório: sem ele o dbt usa o default do dbt_project.yml e
    # transforma uma competência que talvez nem esteja no banco.
    #
    # `dbt build` em vez de `dbt run`: o build roda os 24 testes de dados
    # junto, incluindo assert_evento_nao_existe_na_competencia_anterior — que
    # o README chama de teste sem o qual "o produto perde credibilidade". Com
    # `dbt run` o pipeline terminava "com sucesso" sem validar nada do que
    # produziu. Se um teste falhar agora, o pipeline para: é o comportamento
    # desejado, não uma regressão.
    run_step(
        "3. TRANSFORMAÇÃO DBT (SILVER/GOLD)",
        'dbt build --profiles-dir . --vars '
        f'"{{competencia_atual: \'{args.competencia}\', '
        f'competencia_anterior: \'{args.anterior}\', '
        f'icp_ativo: \'{args.icp}\'}}"',
        cwd="dbt_radar",
    )

    # 4. Enriquecimento com IA (Gemini)
    #
    # Sempre "python -m src.X", nunca "python src/X.py": os módulos de src/
    # usam imports relativos (from . import observabilidade), que só resolvem
    # quando o Python carrega o arquivo como parte do pacote. Rodar o arquivo
    # solto quebra com "attempted relative import with no known parent
    # package". Sem GEMINI_API_KEY o módulo avisa e cai na heurística.
    run_step("4. ENRIQUECIMENTO IA",
             "python -m src.enriquecimento"
             + (" --sem-ia" if args.sem_ia else ""))

    # 5. Geração do Produto Final
    run_step("5. GERAÇÃO DO DIGEST", "python -m src.digest")

    total_time = time.time() - total_start
    print(f"\n🎉 PIPELINE FINALIZADO COM SUCESSO EM {total_time:.2f} SEGUNDOS! 🎉")
    print("Verifique a pasta 'data/digests/' para ver o relatório de hoje.")

if __name__ == "__main__":
    main()
