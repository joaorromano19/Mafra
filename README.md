# Agente NFSe Campinas — MAFRA

Automação para exportar a Relação de Notas Fiscais do portal **NFSe Campinas** para múltiplos clientes da MAFRA ASSESSORIA CONTABIL.

Roda em background (sem janela), usando Chromium embutido do Playwright. Sem dependência de Chrome instalado, sem certificado digital, sem interação manual.

## Como funciona

1. Login único no portal com CNPJ + senha **da MAFRA** (escritório de contabilidade)
2. Para cada cliente listado em `clientes.csv`: seleciona o CNPJ no portal e exporta a relação de notas
3. Re-login automático apenas se a sessão expirar durante o loop

## Instalação

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## Configuração

Copie `.env.example` para `.env` e preencha:

```
CNPJ_PORTAL=11147129000143      # CNPJ da MAFRA (login no portal)
SENHA_PORTAL=sua_senha_aqui     # Senha de acesso da MAFRA
PASTA_RESULTADOS=resultados
ARQUIVO_CLIENTES=clientes.csv
TIMEOUT_MS=30000
```

Crie um `clientes.csv` apenas com os CNPJs dos clientes da MAFRA:

```csv
cnpj,nome
12345678000100,CLIENTE A LTDA
98765432000199,CLIENTE B ME
```

A coluna `nome` é opcional — se omitida, o nome é extraído do portal.

## Uso

```bash
# Mês anterior (padrão)
python main.py

# Competência específica
python main.py --competencia 04/2026

# Intervalo de competência
python main.py --inicio 03/2026 --fim 04/2026

# Arquivo de clientes diferente
python main.py --clientes outro.csv
```

## Saída

Os arquivos baixados ficam em `resultados/<NOME_CLIENTE>/`, com nome no formato:

```
NOME_CLIENTE_MM-AAAA_TIMESTAMP.xml
```

Um relatório CSV consolidado é salvo em `resultados/relatorio_TIMESTAMP.csv`.
