"""
Agente de Exportação NFSe Campinas — Versão 2 (Certificado Digital)
====================================================================
Uso:
    python main.py                          → usa clientes.csv e competência do mês anterior
    python main.py --competencia 04/2026    → competência específica (um mês)
    python main.py --inicio 03/2026 --fim 04/2026   → intervalo de competência
    python main.py --clientes outro.csv     → arquivo de clientes diferente
    python main.py --cert "OUTRO NOME"      → sobrescreve CERT_NAME do .env
"""

import asyncio
import argparse
import logging
import sys
import os
from datetime import datetime, date
from pathlib import Path

import pandas as pd

from config import Config
from agente import NFSeAgente, ClienteNaoEncontradoError

# ------------------------------------------------------------------ logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("execucao.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ helpers

def mes_anterior() -> str:
    hoje = date.today()
    if hoje.month == 1:
        return f"12/{hoje.year - 1}"
    return f"{hoje.month - 1:02d}/{hoje.year}"


def carregar_clientes(caminho: str) -> pd.DataFrame:
    caminho = Path(caminho)
    if not caminho.exists():
        logger.error(f"Arquivo de clientes não encontrado: {caminho}")
        sys.exit(1)

    if caminho.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(caminho, dtype=str)
    else:
        df = pd.read_csv(caminho, dtype=str)

    df.columns = [c.strip().lower() for c in df.columns]

    if "cnpj" not in df.columns:
        logger.error("O arquivo de clientes precisa ter uma coluna 'cnpj'.")
        sys.exit(1)

    df = df.dropna(subset=["cnpj"])
    df["cnpj"] = df["cnpj"].str.strip()
    return df


def imprimir_relatorio(resultados: list[dict]):
    total = len(resultados)
    ok = sum(1 for r in resultados if r["status"] == "ok")
    erros = total - ok

    print("\n" + "=" * 60)
    print(f"  RELATÓRIO FINAL — {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print("=" * 60)
    print(f"  Total de clientes: {total}")
    print(f"  Sucesso:           {ok}")
    print(f"  Erros:             {erros}")
    print("-" * 60)

    for r in resultados:
        icone = "OK" if r["status"] == "ok" else "XX"
        print(f"  {icone}  {r['cnpj']:<20}  {r.get('nome', '')[:30]:<30}  {r.get('mensagem', '')}")

    print("=" * 60 + "\n")


# ------------------------------------------------------------------ main loop

async def executar(args):
    config = Config()

    # Sobrescreve CERT_NAME se passado via CLI
    if args.cert:
        config.CERT_NAME = args.cert

    if not config.CERT_NAME:
        logger.error(
            "CERT_NAME não configurado. Defina no .env ou use --cert \"NOME DO CERTIFICADO\""
        )
        sys.exit(1)

    competencia_inicio = args.inicio or args.competencia or mes_anterior()
    competencia_fim = args.fim or args.competencia or competencia_inicio

    arquivo_clientes = args.clientes or config.ARQUIVO_CLIENTES
    df = carregar_clientes(arquivo_clientes)

    total = len(df)
    logger.info(f"Clientes carregados: {total}")
    logger.info(f"Competência: {competencia_inicio} a {competencia_fim}")
    logger.info(f"Resultados serão salvos em: {config.PASTA_RESULTADOS}/")
    logger.info(f"Certificado: {config.CERT_NAME}")

    resultados = []

    agente = NFSeAgente(config)
    await agente.iniciar()

    try:
        # Login único via certificado digital
        await agente.garantir_login()

        for idx, linha in df.iterrows():
            cnpj = linha["cnpj"]
            numero = idx + 1
            logger.info(f"[{numero}/{total}] Processando CNPJ: {cnpj}")

            resultado = {"cnpj": cnpj, "status": "erro", "nome": "", "mensagem": ""}

            try:
                # Re-verifica sessão antes de cada cliente
                await agente.garantir_login()

                nome = await agente.selecionar_cliente(cnpj)
                resultado["nome"] = nome

                pasta = os.path.join(config.PASTA_RESULTADOS, _pasta_cliente(cnpj, nome))
                arquivo = await agente.exportar_relacao_notas(
                    competencia_inicio=competencia_inicio,
                    competencia_fim=competencia_fim,
                    pasta_destino=pasta,
                    nome_cliente=nome,
                )

                resultado["status"] = "ok"
                resultado["mensagem"] = os.path.basename(arquivo)
                logger.info(f"[{numero}/{total}] OK: {os.path.basename(arquivo)}")

            except ClienteNaoEncontradoError as e:
                resultado["mensagem"] = str(e)
                logger.warning(f"[{numero}/{total}] Cliente não encontrado: {e}")

            except Exception as e:
                resultado["mensagem"] = str(e)
                logger.error(f"[{numero}/{total}] Erro inesperado: {e}", exc_info=True)

            resultados.append(resultado)

    finally:
        await agente.fechar()

    imprimir_relatorio(resultados)
    _salvar_relatorio_csv(resultados, config.PASTA_RESULTADOS)


def _pasta_cliente(cnpj: str, nome: str) -> str:
    nome_limpo = "".join(c if c.isalnum() or c in " _-" else " " for c in nome).strip()
    return nome_limpo or cnpj


def _salvar_relatorio_csv(resultados: list[dict], pasta: str):
    Path(pasta).mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(resultados)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    caminho = os.path.join(pasta, f"relatorio_{ts}.csv")
    df.to_csv(caminho, index=False, encoding="utf-8-sig")
    logger.info(f"Relatório salvo em: {caminho}")


# ------------------------------------------------------------------ CLI

def parse_args():
    parser = argparse.ArgumentParser(
        description="Agente NFSe Campinas v2 — Certificado Digital"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--competencia",
        metavar="MM/AAAA",
        help="Mês de competência (início e fim iguais). Padrão: mês anterior.",
    )
    parser.add_argument(
        "--inicio",
        metavar="MM/AAAA",
        help="Competência início (use junto com --fim para intervalo).",
    )
    parser.add_argument(
        "--fim",
        metavar="MM/AAAA",
        help="Competência fim.",
    )
    parser.add_argument(
        "--clientes",
        metavar="ARQUIVO",
        help="Caminho para o CSV/Excel de clientes. Padrão: clientes.csv",
    )
    parser.add_argument(
        "--cert",
        metavar="NOME",
        help='Sobrescreve CERT_NAME do .env. Ex: "NELSON LUIZ MAFRA JUNIOR:31929038801"',
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(executar(args))
