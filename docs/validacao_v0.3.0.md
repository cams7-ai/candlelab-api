# Validação — CandleLab API 0.3.0

Data: 19/11/2026. Implementação do [prompt](prompt_261119_v0.3.0.md), escrito após análise de Web/API e antes da implementação. A preparação foi feita em cópia temporária, seguida de aplicação dos arquivos no checkout da API.

## Entrega

- CRUD persistente MongoDB de combinações (`preset` e `custom`), nomes únicos por casefold, validação dos parâmetros pelo registro e erros no contrato HTTP atual.
- `combinable` explícito no catálogo, mantendo os 22 itens públicos e suas regras. Somente os 13 IDs declarados compatíveis podem participar da composição livre; futuras estratégias não são habilitadas implicitamente.
- Executor interno com regras independentes all/any no mesmo candle, aquecimento de todos, prioridade SELL, carteira única, callback dos fills a todos os componentes e indicadores prefixados. Stops seguem a regra SELL; sem votos armazenados de candles passados nem acesso ao futuro.
- Criação de processamento por `combination_id`, snapshot completo/imutável, hash de idempotência incluindo configuração e worker independente do cadastro mutável. Edição/exclusão preserva execuções existentes, incluindo tarefas já enfileiradas.
- CORS inclui PUT; OpenAPI, README, pyproject.toml e lockfile atualizados para 0.3.0.

## Verificações

- `.venv/bin/python -m pytest -q -m 'not integration'`: **170 aprovados, 1 ignorado, 3 desmarcados**. Inclui **25 casos novos**, entre testes diretos e parametrizados, e regressões de catálogo, HTTP/worker, causalidade, contabilidade e estratégias anteriores.
- Novos testes cobrem CRUD, validação, duplicação de nome/membros, exclusão/ID inexistente, presets, recursão/estratégias incompatíveis, all/any, conflito BUY/SELL, aquecimento, observação do mesmo candle, callbacks compartilhados, indicadores sem colisões, CORS PUT, snapshots antes de edição/exclusão e execução pelo worker. Também verificam idempotência, ausência de futuro comparando prefixo do backtest, fills na abertura seguinte, taxas e conciliação financeira.
- `ruff format --check` dos nove arquivos Python alterados: aprovado.
- OpenAPI exportado por `create_app().openapi()`, com versão 0.3.0 e 14 caminhos, incluindo cadastro/edição/exclusão.

## Ambiente e limites

O FastAPI TestClient travou na inicialização do loop de eventos dentro do sandbox; os testes foram executados com a permissão de execução fora dele, usando MongoDB simulado e os clientes de rede controlados pelos testes. Não houve acesso a serviços de produção. Há um aviso de depreciação preexistente de `anyio.abc.BlockingPortal` no Starlette.

O teste ignorado depende do arquivo de aceitação externo; os três testes marcados `integration` requerem serviços reais e não foram executados. Não foi feita integração com MongoDB/Redis reais nem publicação. O navegador também estava indisponível para a verificação visual do frontend. Reinicie API e worker com o novo código; o banco cria os índices do cadastro na inicialização normal.

Os snapshots preservam a definição utilizada. Depois de excluir um cadastro, novas solicitações por seu ID retornam 404; consultas e execução de processamentos já criados continuam funcionando. Dados auxiliares de presets são específicos do dataset e têm a cobertura temporal revalidada antes do enfileiramento.
