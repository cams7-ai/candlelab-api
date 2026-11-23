# CandleLab API 0.3.0

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
alterada com `API_PORT`. API, MongoDB e Redis publicam suas portas em todas as
interfaces IPv4 por padrão (`DOCKER_BIND_IP=0.0.0.0`). O volume
`mongo_data` preserva datasets, sessões, tarefas e resultados após reinícios.
`docker compose down` preserva esse volume; `down -v` apaga os dados.

O Compose inicia API, worker, MongoDB e Redis com versões fixadas e health checks.
Redis é opcional para leituras: sua falha não impede upload ou execução offline.
O worker verifica MongoDB no health check e renova a posse das tarefas em andamento.
O endpoint Binance é a única operação da aplicação que consulta serviços externos.

### Iniciar os serviços individualmente

Para iniciar somente MongoDB e Redis em segundo plano, sem subir a API ou o worker:

```bash
cp .env.example .env # Apenas na primeira configuração; preserve um .env existente.
docker compose up -d mongo
docker compose up -d redis
```

Quando quiser iniciar a aplicação, construa a imagem e suba cada serviço separadamente:

```bash
docker compose build api
docker compose up -d api
docker compose up -d worker
```

API e worker compartilham a mesma imagem. O Compose também inicia as dependências
necessárias caso ainda estejam paradas. Para acompanhar ou parar um serviço:

```bash
docker compose logs -f api
docker compose stop api
docker compose stop worker
docker compose stop mongo redis
```

### Conexões pelo host ou por outra máquina

As portas publicadas podem ser configuradas no `.env` com `API_PORT` (8000),
`MONGO_PORT` (27017) e `REDIS_PORT` (6379). Use `localhost` na própria máquina
ou o IP do host Docker para acessar de outra máquina:

| Serviço | Endereço externo padrão |
| --- | --- |
| API | `http://IP_DO_HOST:8000` |
| MongoDB / Compass | `mongodb://IP_DO_HOST:27017/candlelab` |
| Redis | `redis://IP_DO_HOST:6379/0` |

MongoDB e Redis não exigem usuário, senha ou TLS nessa configuração. O Redis
aceita conexões externas com `--protected-mode no`. Restrinja o acesso dessas
portas à rede confiável no firewall. Para limitar todas as portas à própria
máquina, configure `DOCKER_BIND_IP=127.0.0.1`.

Após alterar as portas ou o endereço de publicação, aplique a configuração:

```bash
docker compose up -d api worker mongo redis
```

O worker não possui porta de atendimento. API e worker continuam usando os nomes
internos `mongo:27017` e `redis:6379`, independentemente das portas publicadas.
Para um frontend em outra máquina, inclua sua origem em `CANDLELAB_CORS_ORIGINS`.

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
`1m`, `5m` e `1h` são os intervalos fixos suportados. O passo é uma hipótese do simulador,
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

O campo `name` do dataset guarda o nome original do arquivo JSON (incluindo a extensão).
Na aquisição pela Binance, é gerado como `simbolo_intervalo_inicio_fim`, por exemplo
`btcusdt_1m_2026-11-13T1300Z_2026-11-13T1800Z`, com datas em UTC. Quando uma data
não é informada, usa-se a primeira ou última abertura retornada. Ao reutilizar um
snapshot com o mesmo conteúdo, o nome é atualizado para o da aquisição mais recente;
a identidade e os candles permanecem os mesmos. Conjuntos antigos sem `name` recebem
um nome gerado a partir do par, intervalo e período nas respostas da API.

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
parâmetros, caixa, taxa e passo. O registro em `candlelab/domain/strategies.py`
contém 22 estratégias. As quatro opções originais são:

| ID | Padrões | Regra |
| --- | --- | --- |
| `SMA_CROSS@1` | 9/21 | Cruzamentos estritos com igualdade anterior permitida |
| `EMA_CROSS@1` | 9/21 | Mesmo cruzamento; semente SMA, alpha 2/(N+1) |
| `RSI_REVERSION@1` | 14, 30/70 | Saída da sobrevenda/sobrecompra; Wilder com semente SMA |
| `SMA_RSI_FILTER@1` | 9/21, RSI 14, confirmação 50 | BUY por cruzamento e RSI >= confirmação; SELL por SMA |

O catálogo inclui ainda 12 estratégias individuais e seis combinações, conforme o
[guia de estratégias do CandleLab Web](../candlelab-web/docs/estrategias_v0.2.0.md).
Elas são registradas por `candlelab/domain/tutorial.py`. O endpoint
`GET /api/v1/strategies` publica os parâmetros, limites e padrões efetivos.

### Estratégias individuais adicionais

| ID | Padrões (exceto dados auxiliares) | Regra |
| --- | --- | --- |
| `TREND_FOLLOWING@1` | `{"stop_pct": "3", "short_window": 8, "long_window": 21}` | EMA curta cruza acima da longa e preço acima de ambas; saída ao perder EMA longa. |
| `MOMENTUM@1` | `{"stop_pct": "3", "roc_period": 5, "ema_period": 10, "volume_period": 20}` | ROC positivo crescente, EMA ascendente e volume acima da média anterior; saída na desaceleração. |
| `MEAN_REVERSION@1` | `{"stop_pct": "3", "period": 20, "deviations": "2", "adx_period": 14, "adx_limit": "25"}` | Retorno à banda inferior com ADX baixo; saída na média. |
| `GRID_TRADING@1` | `{"stop_pct": "3", "lookback": 20, "levels": 5}` | Grade congelada na primeira janela; uma ordem por candle e um lote de inventário por ciclo. |
| `BREAKOUT@1` | `{"stop_pct": "3", "lookback": 20, "atr_period": 14, "atr_multiple": "2", "min_atr_pct": "0.1"}` | Fechamento acima da máxima anterior com filtro ATR; saída no nível rompido ou trailing ATR. |
| `SWING_TRADING@1` | `{"stop_pct": "3", "lookback": 20, "min_reward_risk": "2"}` | Reação a suporte anterior com SMA20 e retorno/risco mínimo; saída na resistência. |
| `SCALPING@1` | `{"stop_pct": "3", "max_spread_bps": "10", "target_pct": "0.5", "max_hold": 3}` | Recuperação da VWAP diária UTC com spread estreito e fluxo positivo; alvo/stop/tempo. |
| `ARBITRAGE@1` | `{"min_edge_bps": "1", "transfer_rate": "0"}` | Compra no ask de A e venda simultânea no bid de B, ou direção inversa, se margem líquida positiva. |
| `DCA@1` | `{"every_candles": 24, "start_index": 1}` | Três compras de orçamento igual (caixa inicial/3 incluindo taxa); não força venda. |
| `MARKET_MAKING@1` | `{"stop_pct": "3"}` | Cotação passiva no bid/ask observado; próxima observação confirma fill explícito da ordem anterior. |
| `PAIRS_TRADING@1` | `{"stop_pct": "3", "period": 20, "entry_z": "2", "stop_z": "4", "borrow_rate_per_candle": "0.00001"}` | Z-score causal BTC/ETH: compra BTC e vende ETH a descoberto com nocionais iguais; encerra as duas pernas na normalização/stop. |
| `MACHINE_LEARNING@1` | `{"stop_pct": "3", "buy_probability": "0.6", "sell_probability": "0.5"}` | Compra acima do limiar externo; vende abaixo do limiar de saída ou stop. |

### Combinações de estratégias

| ID | Padrões (exceto dados auxiliares) | Regra |
| --- | --- | --- |
| `EMA_ADX_ATR@1` | `{"stop_pct": "3", "short_window": 8, "long_window": 21, "atr_period": 14, "atr_multiple": "2", "adx_limit": "25"}` | EMA curta acima da longa, ADX forte e confirmação de alta; saída por trailing ATR. |
| `RSI_BOLLINGER@1` | `{"stop_pct": "3", "period": 20, "deviations": "2", "rsi_period": 14, "recovery": "30"}` | RSI cruza recuperação e preço retorna à banda inferior no mesmo candle; saída na média. |
| `BREAKOUT_VOLUME_ATR@1` | `{"stop_pct": "3", "lookback": 20, "atr_period": 14, "atr_multiple": "2", "min_atr_pct": "0.1", "volume_period": 20, "volume_multiple": "1"}` | Rompimento da máxima anterior com volume maior que média anterior; saída no nível/trailing ATR. |
| `MACD_EMA_RSI@1` | `{"stop_pct": "3", "ema_period": 21, "rsi_period": 14, "rsi_min": "40", "rsi_max": "70"}` | MACD12/26 cruza sinal9, preço acima EMA21 e RSI na faixa; saída na perda de força MACD. |
| `GRID_ADX_ATR@1` | `{"stop_pct": "3", "lookback": 20, "levels": 5, "atr_period": 14, "adx_limit": "20", "atr_multiple": "1"}` | Grade em regime ADX baixo, espaçamento ATR congelado; desliga em rompimento ou ADX forte. |
| `MOMENTUM_RANKING@1` | `{"stop_pct": "3", "roc_period": 5, "ema_period": 10, "volume_period": 20, "top_n": 3, "min_liquidity": "1000000", "min_roc": "0"}` | Momentum confirmado e BTC elegível no ranking de ROC de universo líquido; sai quando perde seleção. |

Scalping, Arbitragem, Market Making, Pairs Trading, Machine Learning e Momentum +
Ranking exigem dados auxiliares em `parameters.auxiliary_json`, como string contendo
um array JSON com uma observação por abertura do dataset. Pairs Trading e Momentum +
Ranking exigem dataset BTCUSDT. Machine Learning consome probabilidades externas;
não treina modelos. Consulte os formatos e as hipóteses de execução no
[guia de estratégias e importação](docs/estrategias_v0.2.0.md).

Nas estratégias adicionais, stops percentuais e trailing ATR são avaliados no
fechamento para execução na abertura seguinte. DCA divide o orçamento em três
compras; Grid usa ordens limitadas; Arbitragem simula duas pernas atômicas; Market
Making depende de flags externas de execução; Pairs Trading inclui colateral e
empréstimo de ETH. Essas regras usam `CANDLELAB_ENGINE@2` e
`TUTORIAL_NEXT_OPEN_LIMITS_COLLATERAL@1`. As quatro estratégias originais preservam
`CANDLELAB_ENGINE@1` e `NEXT_OPEN_ALL_IN_KEEP_OPEN@1`.

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

Nas quatro estratégias originais, o sinal fica disponível no fechamento e
preenche na abertura seguinte, sem slippage. O evento sinal ancora o candle avaliado; o fill registra outra âncora,
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

Não há autenticação: a configuração fornecida publica API, MongoDB e Redis em
todas as interfaces IPv4 para acesso em rede confiável. Upload e simulação sobre snapshots existentes funcionam sem internet;
a instalação inicial de dependências e imagens requer acesso aos registries.

## Versão 0.2.0

O [prompt desta versão](docs/prompt_261116_v0.2.0.md) foi criado antes das alterações e executado neste projeto. Consulte o [guia de estratégias e importação](docs/estrategias_v0.2.0.md) para as 18 novas opções, regras, padrões e hipóteses de execução. As quatro opções anteriores permanecem disponíveis. A entrada auxiliar é importada por processamento, com snapshot persistido e validação de cobertura temporal; não há dados externos fabricados automaticamente. Suporte adicional a candles de 1 hora.

Veja os [resultados de validação 0.2.0](docs/validacao_v0.2.0.md).


## Cadastro e execução de combinações — 0.3.0

[Prompt executado](docs/prompt_261119_v0.3.0.md) · [Validação](docs/validacao_v0.3.0.md).

Os cadastros ficam na coleção MongoDB `strategy_combinations`, com índices únicos para ID e nome normalizado por `casefold`. API e worker precisam ser reiniciados com o código 0.3.0; o frontend requer CORS com GET/POST/PUT/DELETE, já configurado. Os 22 itens anteriores do catálogo continuam disponíveis.

- GET `/api/v1/strategy-combinations`: `{ "items": [...], "next_cursor": null }`.
- POST `/api/v1/strategy-combinations`: cria e retorna cadastro (201).
- PUT `/api/v1/strategy-combinations/{id}`: substitui definição validada e retorna cadastro (200).
- DELETE `/api/v1/strategy-combinations/{id}`: exclui somente cadastro (204). Configurações personalizadas em uso retornam 409 `CONFIGURATION_IN_USE`.

Nome: 1–200 caracteres após remover espaços externos; nomes repetidos ignorando caixa retornam 409. ID inexistente retorna 404; dados inválidos retornam 422 no contrato HTTP atual. Não há alteração retroativa de processamentos.

Exemplo de composição livre:

```json
{
  "name": "SMA e EMA",
  "kind": "custom",
  "members": [
    {"strategy_id": "SMA_CROSS@1", "parameters": {"short_window": 5, "long_window": 21}},
    {"strategy_id": "EMA_CROSS@1", "parameters": {"short_window": 8, "long_window": 21}}
  ],
  "buy_rule": "all",
  "sell_rule": "any"
}
```

São aceitos de 2 a 8 membros distintos cujo `combinable` no catálogo seja `true`. Parâmetros são validados/normalizados pelo mesmo registro das execuções diretas. A lista inclui as quatro estratégias legadas, Trend Following, Momentum, Mean Reversion, Breakout, Swing Trading e as combinações EMA/ADX/ATR, RSI/Bollinger, Breakout/Volume/ATR e MACD/EMA/RSI. DCA, grid e estratégias com dados auxiliares/múltiplas pernas não são elegíveis para composição genérica. Não se aceita referência a outro cadastro nem ao executor customizado dentro dos membros.

Cada componente observa o mesmo fechamento e os fills reais da carteira compartilhada. Somente após todos estarem prontos, `all` exige sinal da direção em todos os componentes naquele candle e `any` exige em pelo menos um. Os votos não ficam armazenados de candles anteriores. Quando BUY e SELL satisfazem suas regras simultaneamente, SELL tem prioridade. Stops dos componentes seguem a regra SELL, inclusive quando ela é `all`; a exceção explícita são stops e desativação da própria grade, descritos abaixo. Ordens são executadas na próxima abertura, sem múltiplas carteiras. Composições com grade preservam suas ordens limitadas e parcelas; veja a política de grade abaixo. Indicadores usam prefixo `member_N__`, preservando os aquecimentos como `null`.

Exemplo de configuração salva de uma combinação publicada:

```json
{"name": "EMA ADX ajustada", "kind": "preset", "strategy_id": "EMA_ADX_ATR@1", "parameters": {"short_window": 5, "long_window": 21}}
```

As seis combinações publicadas podem ser usadas como presets, mantendo as políticas originais. Momentum + Ranking continua exigindo `parameters.auxiliary_json` válido e dataset BTCUSDT; a cobertura temporal é revalidada ao iniciar o processamento. Dados auxiliares salvos são específicos de um dataset e não são preenchidos automaticamente.

Para executar, envie a `POST /api/v1/sessions/{id}/runs`:

```json
{"combination_id": "ID_RETORNADO_PELO_CADASTRO"}
```

Esse campo é exclusivo em relação a `strategy_id`; com cadastro, não são aceitos parâmetros sobrescritos nem `auxiliary_filename`. Execuções diretas continuam aceitando os campos anteriores. O Service resolve o cadastro uma vez e persiste `combination_snapshot` completo, parâmetros efetivos e políticas. Composição livre usa o executor interno `CUSTOM_COMBINATION@1`, com JSON canônico em `parameters.definition_json`, motor `CANDLELAB_COMPOSITION@1` e política `SAME_CLOSE_VOTES_NEXT_OPEN@1`; ele não aparece como opção adicional no catálogo. O worker executa o snapshot, independentemente de edições no cadastro. A exclusão do cadastro fica bloqueada enquanto houver processamentos vinculados; snapshots de cadastros removidos por versões anteriores continuam executáveis. Presets continuam usando o ID original da estratégia.

O snapshot entra no hash de idempotência: a mesma chave e configuração retornam a execução anterior; uma configuração alterada com a mesma chave retorna 409. Após excluir o cadastro, novas solicitações por seu ID retornam 404, inclusive tentativas de criação com chave anterior; a consulta da execução antiga continua disponível. Contabilidade Decimal, taxas e conciliação são preservadas.


### Componentes reutilizáveis e agrupamento de combinações

As seis receitas publicadas usam regras compartilhadas de sinal, filtro e saída em `candlelab/domain/components.py`. Os identificadores, parâmetros, aquecimento, valores e regras anteriores são preservados. A confirmação RSI legada também compartilha sua função com o novo componente individual. O catálogo mantém as 22 estratégias e acrescenta 12 componentes; `composition_role` descreve seu papel e `components` descreve as receitas.

Uma combinação livre pode incorporar outra definição em `members[].combination`. O grupo é um snapshot completo, não uma referência ao cadastro de origem. Regras e agrupamentos são preservados; editar a origem não altera o grupo nem execuções históricas. A origem não pode ser excluída enquanto houver vínculos ou cópias cuja origem não possa ser descartada. Membros antigos com `strategy_id` e `parameters` continuam válidos. Membros de grupo deixam esses dois campos vazios; misturar as duas formas é rejeitado.

Filtros autorizam compras independentemente de `all`/`any` e não geram votos de venda. Componentes de saída só participam das vendas. Ao menos um membro de sinal é obrigatório. Grupos seguem os mesmos requisitos de aquecimento e recebem os fills da carteira compartilhada. Há até 8 membros distintos por grupo, 4 níveis e 64 folhas executáveis no total (uma receita publicada conta como uma folha). Snapshots incorporados não criam ciclos de dependência.

Grid Trading e Grid + ADX + ATR podem ser incorporados a combinações livres, inclusive por snapshots e grupos. A árvore aceita uma única grade. O executor preserva preço limite e parcelas da grade; filtros autorizam compras e outros sinais não podem abrir uma posição sem a grade, mesmo com `buy_rule=any`. Saídas adicionais a mercado não herdam limites de venda. Stops e desativação da própria grade encerram o inventário mesmo com `sell_rule=all` ou outros membros em aquecimento. A receita Grid + ADX + ATR mantém seu espaçamento ATR e a desativação por ADX; Grid Trading com filtros e saídas individuais não promete equivalência automática. DCA, dados externos e carteiras especiais não podem ser incorporados a uma combinação livre. Essas estratégias continuam disponíveis no processamento e, quando aplicável, como configurações publicadas.

A regressão compara 110 digests de execuções completas capturados antes da refatoração, além de 16 comparações financeiras entre receitas e composições explícitas dos componentes. Veja `tests/test_composition_regression.py` e `tests/test_nested_combinations.py`. Reinicie API, worker e frontend juntos após atualizar; não é necessária migração dos cadastros existentes.


### Configurações personalizadas

`POST/PUT /api/v1/strategy-combinations` também aceita `kind: strategy`, com `strategy_id` de uma estratégia individual/clássica e seus parâmetros. O tipo mantém membros vazios e regras `buy_rule: all` / `sell_rule: any`, como nas configurações `preset`. Receitas publicadas continuam usando `preset`; as combinações livres continuam usando `custom`. Valores são normalizados e validados pelo mesmo schema do catálogo, inclusive dados auxiliares.

A criação de processamentos já aceita o ID do cadastro via `combination_id`, com snapshot persistido. Configurações compatíveis também podem ser incorporadas a combinações como snapshots, preservando o papel de sinal, filtro ou saída do componente. Não há migração de cadastros anteriores. Atualize API, worker e frontend juntos. Os testes de CRUD, execução, validação, dados externos, preservação de decimais e histórico estão em `tests/test_strategy_configurations.py`.


### Restrição de exclusão de configurações e combinações cadastradas

A exclusão de qualquer cadastro (`kind: strategy`, `custom` ou `preset`) verifica os processamentos de todas as sessões e as cópias incorporadas em combinações, inclusive grupos aninhados. Um processamento ligado diretamente ao cadastro é identificado pelo ID preservado em `combination_snapshot`, mesmo após editar o nome ou os parâmetros da origem. Todos os estados de processamento impedem a exclusão enquanto o registro existir.

Quando há vínculo, DELETE `/api/v1/strategy-combinations/{id}` responde HTTP 409 com `error.code: CONFIGURATION_IN_USE` para configurações personalizadas e `COMBINATION_IN_USE` para combinações livres ou receitas salvas. A lista `error.details` identifica `runs`, `combinations` e `uncertain_copies`. O cadastro não é removido. Depois de remover os registros que impedem a exclusão, a mesma operação pode retornar 204. Um ID inexistente continua retornando 404.

O contrato atual dos grupos armazena cópias sem ID de origem. Uma cópia de `kind: strategy` com a mesma estratégia base e o mesmo nome identifica o vínculo; com outro nome, não é possível descartar uma origem alterada. Nesse caso a API também retorna 409 e informa a cópia ambígua. Cadastros diferentes identificados diretamente nos snapshots de processamentos não são confundidos apenas por usarem a mesma estratégia base. A regra é aplicada na camada de armazenamento, independentemente do frontend. A mesma verificação protege receitas salvas e composições livres. Grupos `preset` são comparados pelo tipo e ID da receita; grupos `custom`, pelo nome ou conjunto de IDs das estratégias base, percorrendo os membros aninhados. Cópias de mesmo nome são tratadas como vínculos; cópias da mesma base com outro nome são ambíguas e impedem a exclusão, conforme a interface. A comparação não depende dos parâmetros ou da ordem dos membros. Processamentos de outro ID de cadastro não impedem a exclusão apenas por usarem a mesma base. Falhas de consulta impedem a remoção e continuam retornando 503.


### Exclusão de conjuntos de candles

DELETE `/api/v1/datasets/{id}` retorna HTTP 409 com `DATASET_IN_USE` se houver qualquer sessão vinculada ao conjunto. A resposta identifica as sessões em `error.details[].sessions`. A API preserva os candles, as sessões, os processamentos e os resultados; não há exclusão em cascata a partir dos candles. Depois de remover as sessões por suas ações próprias, um conjunto sem vínculos pode ser excluído com resposta 204. IDs inexistentes retornam 404.

A verificação acontece antes e depois de marcar o conjunto para exclusão, em conjunto com a validação da criação de sessões. Uma sessão criada durante a checagem impede a exclusão e o conjunto volta a ficar disponível. Falhas na consulta também não autorizam remover os dados.


### Exclusão de sessões

DELETE `/api/v1/sessions/{id}` retorna HTTP 409 com `SESSION_IN_USE` enquanto houver qualquer processamento vinculado, independentemente de seu estado. Os IDs e estados dos vínculos são informados em `error.details[].runs`. A exclusão não remove processamentos, séries, resultados ou relatórios em cascata. Remova os processamentos por suas próprias ações antes de excluir uma sessão; cadastros sem vínculos retornam 204 e IDs inexistentes retornam 404.

A proteção é aplicada na camada de armazenamento, com nova verificação após bloquear a criação de processamentos. Vínculos criados durante a validação ou falhas na consulta mantêm a sessão e seus dados disponíveis.


Verificação da atualização de exclusão (21/11/2026): os testes HTTP/armazenamento abrangem os três tipos de cadastro e todos os estados de processamento, grupos aninhados, cópias ambíguas, snapshots históricos, consultas sem limite de página e falhas do banco. O formato de erro e os endpoints existentes foram preservados; nenhuma migração de dados ou mudança de versão é necessária. A versão permanece 0.3.0.


### Paridade entre receitas e componentes

`GRID_TRADING@1` expõe `spacing_mode` (`range` ou `atr`), `atr_period` e `atr_multiple` (Espaçamento ATR). `GRID_REGIME@1` expõe `regime_action` (`filter` ou `disable_grid`). Os padrões `range` e `filter` mantêm as definições anteriores. Para reproduzir `GRID_ADX_ATR@1`, use os dois componentes com as opções `atr` e `disable_grid`, os mesmos parâmetros numéricos e regras all/any. O regime é avaliado antes da grade; a inicialização aguarda seu aquecimento, e o ADX alto desativa a grade permanentemente e encerra a posição a mercado. A árvore aceita uma única grade e a ação de desativação exige uma grade no mesmo grupo, inclusive em grupos internos.

O catálogo publica opções específicas da receita em `components[].parameter_overrides`, além dos parâmetros numéricos da própria receita. Os campos enum publicam nomes de exibição em `x-enum-labels`. A interface permite escolher, validar e persistir essas opções na criação, edição e nos ajustes por execução.

`tests/test_recipe_parity.py` verifica a cobertura de parâmetros de todas as seis receitas e da SMA com RSI, além da equivalência financeira das cinco receitas OHLCV e da SMA com RSI em cenários determinísticos e trajetórias reproduzíveis com parâmetros modificados. Momentum + Ranking continua exigindo dados externos `universe` e não participa da composição livre OHLCV. A receita também encerra posições ao perder elegibilidade, enquanto o componente individual de ranking atua como filtro de compra; não se declara equivalência com uma composição livre. Os 110 digests históricos das receitas e estratégias publicadas continuam preservados.
