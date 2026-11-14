# Validação — CandleLab API

Data: 14/11/2026. Python efetivo: **3.12.14**. Docker Engine: **28.1.1**.
Implementação nova em repositório independente; nenhum commit foi criado.

## Resultados observados

| Verificação | Resultado |
| --- | --- |
| Suíte completa com MongoDB/Redis reais e arquivo de aceitação | **92 passed**, código 0 |
| Testes sem configurar serviços/anexo externos | 85 passed, 3 skipped antes dos quatro últimos testes de limites/lease |
| `ruff check candlelab tests scripts` | Código 0 |
| `ruff format --check candlelab tests scripts` | Código 0, 25 arquivos |
| `git diff --check` | Código 0 |
| `docker compose -p candlelab-validation config --quiet` | Código 0 |
| Build e subida do Compose | Código 0, quatro serviços saudáveis |
| Fluxo HTTP real upload → sessão → run → worker → séries | Código 0 |
| Upload, backtest e leitura de candles com Redis parado | Código 0 |
| Recriação de todos os containers com o mesmo volume MongoDB | Código 0, datasets/sessão/run/resumo preservados |
| Contrato exportado `docs/openapi.json` igual ao `app.openapi()` | Conferido por igualdade de objetos JSON |

A suíte completa foi executada com:

```bash
CANDLELAB_TEST_MONGO_URI=mongodb://172.18.0.3:27017 \
CANDLELAB_TEST_REDIS_URL=redis://172.18.0.2:6379/0 \
CANDLELAB_ACCEPTANCE_FILE=/home/cams7/Dev/Cripto_Tests/replayquant/data/binance/candles-btcusdt-spot-5m_2026-08-19T0000Z_a_2026-08-22T0000Z.json \
.venv/bin/pytest -q -o faulthandler_timeout=20
```

Os IPs acima pertenciam à rede isolada desse ensaio; não são configuração fixa do
projeto. Os testes de integração criam e removem bancos com nomes aleatórios.

A primeira execução no sandbox ficou bloqueada na comunicação entre threads do
TestClient. A mesma suíte fora dessa restrição terminou normalmente. A instalação
de dependências e o acesso ao Docker também precisaram do ambiente sem essa
restrição. Nenhuma dessas limitações foi contornada alterando os contratos.

Houve um aviso de depreciação de `anyio.abc.BlockingPortal` vindo do TestClient do
Starlette; não afetou os testes. As dependências efetivamente usadas estão travadas
em `uv.lock` e `requirements.lock`.

## Cobertura

- Parser estrito das 12 posições, OHLC, não finitos, strings decimais, timestamps,
  alinhamento, intervalo incorreto, duplicatas, ordem, lacunas e limites de entrada.
- Hash canônico, reimportação idempotente, procedências upload/API, isolamento de
  concorrência, retomada de lotes e invisibilidade de importações incompletas.
- Binance simulada: requisição mínima, cada data isolada, datas completas,
  preservação de segundos, limite inválido, inclusão da abertura final, paginação,
  página vazia, não avanço, lacunas, candles abertos, limite total, falha de página,
  retentativa, timeout, `Retry-After`, resposta inválida e reutilização de snapshot.
- SMA/EMA/RSI, sementes, Wilder, igualdade, aquecimento, período/janela 1,
  confirmação/bloqueio SMA+RSI e saída independente do RSI.
- Próxima abertura, vínculo sinal/fill, sinal final sem execução, posição aberta,
  quantidade nula, nenhum trade, taxas, ledger, realizado/não realizado, drawdown,
  BTCUSDT/ETHUSDT, contexto decimal externo e reprodução determinística.
- Contrato HTTP/OpenAPI, validação financeira, paginação e recortes, catálogo,
  idempotência concorrente, isolamento de runs e respostas de polling.
- Tomada atômica, expiração, retomada, heartbeat durante o motor, resultados parciais
  ocultos e revogação da posse inclusive após escrever séries e antes de publicar.
- MongoDB real para concorrência/idempotência/recuperação; Redis real para TTL,
  reaproveitamento e reconstrução de cache inválido. Falha de Redis também simulada
  na suíte HTTP e verificada com o container realmente parado.

## Arquivo de aceitação

O arquivo não estava neste checkout, mas foi localizado no checkout local do
ReplayQuant, no caminho indicado acima. Foi lido sem copiar dados ou assets para
este projeto. O teste opcional confirmou:

- 865 candles BTCUSDT/5m;
- primeira abertura `1787097600000` (`2026-08-19T00:00:00Z`);
- última abertura `1787356800000` (`2026-08-22T00:00:00Z`);
- último fechamento `1787357099999` (`2026-08-22T00:04:59.999Z`);
- upload com `1m` rejeitado com 422, sem outro dataset publicado.

As fixtures pequenas deste repositório são sintéticas e originais.

## Ensaio HTTP em containers

```bash
docker compose -p candlelab-validation up --build -d --wait
.venv/bin/python scripts/smoke_http.py
docker compose -p candlelab-validation stop redis
.venv/bin/python scripts/smoke_http.py
docker compose -p candlelab-validation down
docker compose -p candlelab-validation up -d --wait
.venv/bin/python scripts/smoke_http.py --verify-restart
docker compose -p candlelab-validation ps
```

Também foi feita uma leitura HTTP de `/datasets/{id}/candles` com Redis desligado,
com 200 e os 12 candles da fixture. A carteira sintética SMA 2/3 terminou com caixa
`1460.79325`, posição `0`, taxas `4.65675`, realizado `460.79325`, duas compras e duas
vendas. Esses valores verificam o código com preços artificiais, sem inferência
sobre retorno em mercado real.

O ensaio de reinício preservou datasets, sessão, run e resumo. Os quatro serviços
voltaram saudáveis. Ao final, o stack isolado de teste foi encerrado com `down`,
sem apagar seu volume, deixando a porta 8000 livre para o uso normal.

## Limites da validação

A aquisição real pela Binance não foi disparada: o contrato externo foi consultado
na documentação oficial e todos os casos de aquisição foram testados com respostas
simuladas. Python 3.11 e outras arquiteturas não foram executados; a referência
verificada é Python 3.12.14 em Linux x86_64. Não houve teste de carga com o limite
máximo configurável de um milhão de candles. O motor mantém os dados de uma run
em memória e a persistência divide os resultados em documentos/lotes.

Não há mudanças pendentes conhecidas no escopo funcional solicitado. Estratégias
além das quatro iniciais, autenticação e negociação real ficam fora desta versão.
