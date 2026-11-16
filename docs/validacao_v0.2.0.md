# Validação — 0.2.0

Data: 16/11/2026. Prompt executado: [prompt_261116_v0.2.0.md](prompt_261116_v0.2.0.md).

- 22 opções publicadas: quatro legadas, 12 individuais e seis combinações do tutorial.
- Suíte completa com MongoDB 7.0.16 e Redis 7.4.2 temporários em portas isoladas: **135 passed, 1 skipped**. Inclui execução e persistência das 18 estratégias, causalidade sob alteração de candles futuros, determinismo, indicadores manuais, parcelas DCA, limites grid, fills passivos, duas pernas/emprestimo, margem líquida de arbitragem e validação de auxiliares.
- Teste ignorado: arquivo externo de aceitação com 865 candles não presente. A suíte registra o caminho esperado em `tests/fixtures` / `CANDLELAB_ACCEPTANCE_FILE`.
- HTTP real em Uvicorn: upload dos 40 candles de exemplo, sessão, enfileiramento das 18 estratégias, worker, consulta de resumos e séries e preservação dos parâmetros. Todas concluídas. Nova conexão com MongoDB recuperou resultados.
- `ruff check candlelab tests scripts` e `ruff format --check candlelab tests scripts`: aprovados.
- `uv lock --check --offline --python .venv/bin/python`: aprovado; dependências não mudaram.
- `docker compose -p candlelab-v020-validation config --quiet`: aprovado.
- `docker build -t candlelab-api:0.2.0 .`: aprovado. Container temporário com `--network none` confirmou versão 0.2.0 e 22 estratégias.
- OpenAPI exportado de `create_app().openapi()`; versão 0.2.0. JSON Schema auxiliar e catálogo Web derivados do registro da API.
- `git diff --check`: aprovado.

Comando da suíte:

```bash
CANDLELAB_TEST_MONGO_URI=mongodb://127.0.0.1:27029 \
CANDLELAB_TEST_REDIS_URL=redis://127.0.0.1:6389/0 \
.venv/bin/python -m pytest -q -p no:cacheprovider
```

O aviso de depreciação `BlockingPortal` vem da dependência Starlette/AnyIO.
Não houve consulta externa à Binance, ordens reais ou alteração de banco existente. Os serviços e o banco usados nesta validação foram criados exclusivamente para teste e removidos ao final.

As regras executáveis, custos e limites do simulador estão em [estrategias_v0.2.0.md](estrategias_v0.2.0.md). Os dados auxiliares são importados; ML não treina modelos. Flags de execução passiva, universo de moedas e preços de mercados distintos dependem da qualidade e disponibilidade temporal da fonte. Declarações de treinamento não comprovam ausência de viés.
