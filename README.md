# CandleLab API

Backend local e monousuário para importar candles Binance Spot e executar backtests
reproduzíveis com carteiras independentes. Python 3.11+, FastAPI/Pydantic, PyMongo e
redis-py. Nenhuma credencial Binance é necessária. Todas as operações são simuladas.
O frontend CandleLab Web é independente; o contrato HTTP é `/api/v1`.

## Iniciar

```bash
cp .env.example .env
docker compose up --build
```

API: <http://localhost:8000>, Swagger: <http://localhost:8000/docs>, contrato:
<http://localhost:8000/openapi.json>. `GET /health` verifica MongoDB. A porta pode ser
alterada com `API_PORT`. MongoDB e Redis ficam na rede interna do Compose. O volume
`mongo_data` preserva datasets, sessões, tarefas e resultados após reinícios.
`docker compose down` preserva esse volume; `down -v` apaga os dados.

O Compose inicia API, worker, MongoDB e Redis com versões fixadas e health checks.
Redis é opcional para leituras: sua falha não impede upload ou execução offline.
O worker verifica MongoDB no health check e renova a posse das tarefas em andamento.
O endpoint Binance é a única operação da aplicação que consulta serviços externos.

## Desenvolvimento

Com [uv](https://docs.astral.sh/uv/) instalado:

```bash
uv sync --frozen --python 3.12
uv run uvicorn candlelab.api:app --reload --port 8000
# Em outro terminal, com MongoDB disponível:
uv run python -m candlelab.worker
uv run pytest -m 'not integration' -q
uv run ruff check candlelab tests scripts
uv run ruff format --check candlelab tests scripts
```

`uv.lock` trava desenvolvimento e produção. `requirements.lock` exporta produção
com hashes, usados pelo Docker. Para atualizar intencionalmente:

```bash
uv lock
uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file requirements.lock
```

Configuração: `.env.example`, prefixo `CANDLELAB_`. Listas usam JSON. Para outros
pares Spot cotados em USDT, configure `CANDLELAB_INSTRUMENTS` com `symbol`,
`base_asset`, `quote_asset: "USDT"`, `intervals`, `quantity_step` e `version`.
`1m` e `5m` são os intervalos fixos suportados. O passo é uma hipótese do simulador,
sem afirmar equivalência com os filtros atuais da exchange. A execução guarda o
passo e sua versão; alterações posteriores no catálogo não mudam tarefas criadas.
Origens permitidas pelo navegador estão em `CANDLELAB_CORS_ORIGINS`.

## Usar por HTTP

```bash
curl -s http://localhost:8000/api/v1/instruments
curl -s http://localhost:8000/api/v1/strategies
curl -s -X POST http://localhost:8000/api/v1/datasets \
  -F symbol=BTCUSDT -F interval=5m \
  -F file=@tests/fixtures/btcusdt-5m.json
```

O upload exige JSON Binance de 12 posições, símbolo e intervalo. O símbolo é
normalizado para maiúsculas e `display_symbol` informa `BTC/USDT` ou `ETH/USDT`.
O parser valida tipos estritos, OHLC, volumes, timestamps, alinhamento, duração,
duplicatas, ordem e continuidade. Diagnósticos usam índices de linha base zero.
Limites padrão: arquivo de 32 MiB, 100.000 candles. A camada HTTP também limita o
corpo completo a esse tamanho mais 64 KiB de envelope multipart.

Consulta sob demanda à Binance, JSON mínimo:

```json
{"symbol":"BTCUSDT","interval":"5m"}
```

```bash
curl -s -X POST http://localhost:8000/api/v1/datasets/binance \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"BTCUSDT","interval":"5m"}'
```

Consulta com período e limite por página:

```json
{"symbol":"BTCUSDT","interval":"5m","startTime":"2026-08-19T00:00:00Z","endTime":"2026-08-22T00:00:00Z","limit":1000}
```

```bash
curl -s -X POST http://localhost:8000/api/v1/datasets/binance \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"BTCUSDT","interval":"5m","startTime":"2026-08-19T00:00:00Z","endTime":"2026-08-22T00:00:00Z","limit":1000}'
```

A chamada externa inicial correspondente é:

```bash
curl --request GET --url 'https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=5m&startTime=1787097600000&endTime=1787356800000&limit=1000'
```

O backend mantém esse endpoint público fixo e converte somente datas presentes,
exigindo `YYYY-MM-DDTHH:mm:ssZ`, calendário válido e época Unix ou posterior.
Sem `limit`, ele permanece ausente e a Binance usa seu padrão de até 500 candles.
Sem as duas datas, há uma página: recentes, a partir do início, ou até o fim,
conforme os filtros enviados. Com ambas, pagina até exceder `endTime` inclusivo ou
receber página vazia. O próximo início é a última abertura mais a duração.
Somente candles já fechados no instante inicial da aquisição entram no snapshot.
Timeout e retentativas são limitados; `Retry-After` é respeitado. Se a espera exigida
exceder o orçamento configurado, retorna 503 para nova consulta posterior.
Falhas de página ou sequência não publicam datasets parciais.

Copie o `id` do dataset retornado e crie uma sessão:

```bash
curl -s -X POST http://localhost:8000/api/v1/sessions \
  -H 'Content-Type: application/json' \
  -d '{"name":"BTC 5m","dataset_id":"DATASET_ID","initial_cash":"1000","fee_rate":"0.001"}'
curl -s -X POST http://localhost:8000/api/v1/sessions/SESSION_ID/runs \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: exemplo-sma-1' \
  -d '{"strategy_id":"SMA_CROSS@1","parameters":{"short_window":9,"long_window":21}}'
curl -s http://localhost:8000/api/v1/runs/RUN_ID
curl -s 'http://localhost:8000/api/v1/runs/RUN_ID/series?kind=indicators&limit=100'
curl -s 'http://localhost:8000/api/v1/runs/RUN_ID/series?kind=events&limit=100'
curl -s 'http://localhost:8000/api/v1/runs/RUN_ID/series?kind=equity&limit=100'
```

O `POST` de execução retorna 202; faça polling de `queued`, `running`, `completed`
ou `failed`. Séries ficam disponíveis após `completed`. Sessões são imutáveis:
novos valores de caixa/taxa exigem nova sessão. Cada run começa novamente com o
caixa inicial e posição zero. A mesma chave por sessão reutiliza a run quando a
estratégia e os parâmetros efetivos são iguais; outra requisição recebe 409.

Listagens de datasets, sessões e runs usam `{items,next_cursor}`. Aceitam `limit`
de 1 a 1000 e `cursor` opaco retornado na página anterior. Em candles e séries,
`from_ms` é inclusivo e `to_ms` exclusivo. O cursor pertence à mesma consulta e
filtros; o tamanho da página pode mudar. O limite de listagem é independente do
limite de consulta Binance. A lista de runs usa `/sessions/{id}/runs`.

Erros têm `{error:{code,message,details}}`: 422 para validação, 404 para recurso
ausente, 409 para idempotência/séries indisponíveis, 502 para resposta externa
inválida e 503 para indisponibilidade de dependência. Um run com falha guarda esse
mesmo envelope em `error`. Valores financeiros/indicadores/percentuais são strings
decimais canônicas; timestamps `_ms` são inteiros UTC. `"2.5"` em `_pct` significa
2,5 pontos percentuais. Taxa `"0.001"` é 0,1%.

## Motor e estratégias

O domínio não importa HTTP, MongoDB ou Redis. `simulate` recebe candles, estratégia,
parâmetros, caixa, taxa e passo. O registro em `domain/strategies.py` contém:

| ID | Padrões | Regra |
| --- | --- | --- |
| `SMA_CROSS@1` | 9/21 | Cruzamentos estritos com igualdade anterior permitida |
| `EMA_CROSS@1` | 9/21 | Mesmo cruzamento; semente SMA, alpha 2/(N+1) |
| `RSI_REVERSION@1` | 14, 30/70 | Saída da sobrevenda/sobrecompra; Wilder com semente SMA |
| `SMA_RSI_FILTER@1` | 9/21, RSI 14, confirmação 50 | BUY por cruzamento e RSI >= confirmação; SELL por SMA |

Janelas/períodos são inteiros de 1 a 10.000, curta < longa. O RSI precisa de N+1
fechamentos; cruzamentos exigem duas observações prontas. Aquecimento usa `null`;
execuções sem prontidão têm diagnóstico `INSUFFICIENT_WARMUP`. RSI plano vale 50,
só altas 100 e só baixas 0. Os parâmetros efetivos publicados na run permitem ao
frontend desenhar os limiares do RSI.

Para cadastrar uma estratégia, defina um modelo de parâmetros, implemente
`on_close(candle) -> Evaluation(values, ready, side, reason)` e registre um novo
`StrategySpec` com ID versionado e fábrica de instância nova. Indicadores existentes
são reutilizáveis. A estratégia só observa fechamentos; não recebe carteira.
Declare gatilho, filtros e saída; teste aquecimento e causalidade. Preserve versões
anteriores e seus padrões. O catálogo HTTP deriva do registro e do JSON Schema dos
parâmetros. Novas estratégias não exigem alterações no motor financeiro.

O sinal fica disponível no fechamento e preenche na abertura seguinte, sem
slippage. O evento sinal ancora o candle avaliado; o fill registra outra âncora,
preço e timestamp, vinculado por `signal_id`. Último sinal: `NO_FUTURE_OPEN`.
BUY com posição aberta, SELL sem posição e quantidade nula ficam diagnosticados.
BUY calcula o piso exato de `caixa/(open*(1+taxa))` no passo e debita bruto+taxa.
SELL liquida a posição e credita bruto−taxa. Só fills geram movimentos no ledger.
A posição final permanece aberta e é marcada pelo último fechamento.

A curva tem um ponto por fechamento; seu pico inicial é o caixa inicial. Drawdown
máximo é calculado nessa curva. Realizado é venda líquida menos custo de entrada
com taxa; não realizado é marcação menos custo aberto, sem taxa hipotética de
saída. Sempre é verificado `patrimônio_final − caixa_inicial = realizado + não_realizado`.
A taxa de acerto é `null` sem operações encerradas. Contagens são inteiros.

Política `DECIMAL50_HALF_EVEN_MONEY120_V1`: entradas sem expoente, até 24 dígitos,
12 casas, módulo até 10^12. Recorrências de indicadores usam Decimal com precisão
50 e HALF_EVEN, isolado do contexto externo. Essa precisão integra a decisão;
a serialização não arredonda novamente. Contabilidade usa precisão 120 e rejeita
aritmética inexata. Patrimônio/caixa são limitados a 10^48 USDT e posição a 10^60
unidades; excesso falha explicitamente. Razões percentuais usam precisão 120 com
HALF_EVEN. Piso da quantidade e comparação de drawdown usam razões exatas, sem
float. A run preserva versões do motor, execução, passo e política numérica.

## Persistência e recuperação

- `datasets` mantém a identidade única origem/par/intervalo/hash canônico e estados
  `importing`/`published`; `candles` guarda documentos individuais, com índice único
  `(dataset_id,open_time_ms)` e lotes configuráveis.
- O conteúdo inteiro é validado antes da escrita. IDs de linhas determinísticos e
  imutáveis permitem completar lotes interrompidos repetindo a mesma importação.
  Somente após a contagem integral o snapshot é publicado; concorrentes convergem
  para o mesmo dataset. Importações abandonadas ficam ocultas e podem ser retomadas.
- `acquisitions` guarda todas as procedências, separadas para evitar um documento
  crescente: upload com nome/hash do arquivo ou API com parâmetros efetivos e corte
  temporal. Upload e API que normalizam para o mesmo snapshot reutilizam a identidade.
- `sessions` e `runs` guardam configurações e snapshots; `series` guarda documentos
  individuais por run/tentativa/tipo. Inclui indicadores, eventos, patrimônio, ledger
  e avaliações com prontidão, motivo e vínculo ao sinal.
- O worker toma tarefas atomicamente, renova lease a cada 1/3 do prazo e identifica
  cada tentativa. Resultados são isolados por tentativa. Conclusão/falha exigem posse
  vigente; worker antigo não publica sobre o novo. Tarefa `running` com lease expirado
  é retomada. Séries de tentativas incompletas permanecem ocultas para auditoria.
- Redis só guarda páginas de candles imutáveis com TTL. Queda, perda de cache ou
  JSON inválido provocam leitura pelo MongoDB; tarefas e resultados nunca dependem
  do cache. O Compose limita sua memória e permite descarte por LRU.

## Referências e validação

Consultei o ReplayQuant local no commit `92eb2dd0bcef9fe22b430cf57108bfb32b473a57`
(README, motor, SMA, métricas e testes da Sprint 08) e as especificações
[Sprint 08](https://github.com/cams7-ai/replayquant/blob/main/docs/sprint08.md),
[Sprint 10](https://github.com/cams7-ai/replayquant/blob/main/docs/sprint10.md),
[Sprint 11](https://github.com/cams7-ai/replayquant/blob/main/docs/sprint11.md) e
[Sprint 13](https://github.com/cams7-ai/replayquant/blob/main/docs/sprint13.md)
no HEAD remoto `a31b40f395f3fc74015dd3838c6e5abcee183b4d`.
Regras adaptadas: próxima abertura causal, igualdade em cruzamentos, sementes
EMA/RSI, instrumentos por catálogo, posição preservada, ledger e reconciliação,
ausência de taxa de saída hipotética e procedência de snapshots. Nesta API, a compra
usa todo o caixa com piso, não o orçamento de risco do ReplayQuant. Indicadores
usam Decimal50 versionado em vez de recorrências Fraction. SQLite/CSV da referência
são substituídos por MongoDB/JSON Binance. Não copiei código nem assets: o repositório
consultado não declara licença geral. A implementação e as fixtures pequenas são
originais deste projeto.

Também foram consultados os contratos oficiais de
[klines](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints#klinecandlestick-data)
e da [API Spot](https://developers.binance.com/docs/binance-spot-api-docs/rest-api).
A especificação solicitada está em [docs/prompt_261114.md](docs/prompt_261114.md).
O registro de comandos e resultados fica em [docs/validacao.md](docs/validacao.md).

As fixtures pequenas BTCUSDT e ETHUSDT são sintéticas e verificam comportamento,
sem representar dados da exchange. O anexo de 865 candles não é distribuído neste
checkout. Forneça o JSON em `tests/fixtures/candles-btcusdt-spot-5m_2026-08-19T0000Z_a_2026-08-22T0000Z.json`
ou informe o caminho em `CANDLELAB_ACCEPTANCE_FILE` para o teste de aceitação opcional.
Ele confere primeira/última abertura, quantidade e rejeição de `1m`.

Os testes unitários/HTTP simulam Binance e MongoDB; integração real é habilitada
por `CANDLELAB_TEST_MONGO_URI` e `CANDLELAB_TEST_REDIS_URL` (banco temporário isolado):

```bash
CANDLELAB_TEST_MONGO_URI=mongodb://localhost:27017 \
CANDLELAB_TEST_REDIS_URL=redis://localhost:6379/0 uv run pytest -m integration -q
CANDLELAB_ACCEPTANCE_FILE=/caminho/arquivo.json uv run pytest tests/test_acceptance.py -q
```

Não há autenticação: a configuração fornecida publica somente em localhost para
uso local. Upload e simulação sobre snapshots existentes funcionam sem internet;
a instalação inicial de dependências e imagens requer acesso aos registries.
