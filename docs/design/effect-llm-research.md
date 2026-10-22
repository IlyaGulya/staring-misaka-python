# LLM-слой для grammY + Effect: ограниченное исследование

Проверено 2026-09-08. Область: один schema-validated spam classifier, без автономного агента. Persistence, порядок обработки, миграция и ограничения Telegram остаются за основным дизайном. Установок, выполнения SDK и вызовов моделей не было. Проверены npm metadata и исходники внутри опубликованных npm tarball; `main` не считается доказательством поведения выпущенной версии.

## Рекомендация

**Выбор для этого проекта: Effect 3 + официальный `@effect/ai` за сервисом `SpamClassifier`.** Приоритет — единая модель зависимостей, ошибок и отмены. Принимаем локальную проверку схемы после forced tool call: невалидный ответ переводится в явную ошибку классификации и не разрешает модерацию или снятие наблюдения. Частоту таких ошибок и качество классификации нужно измерить до переключения. Нативный constrained decoding пока не является требованием проекта. [Effect 3 adapter][e3-provider], [Effect 3 LanguageModel][e3-ai]

**Если требуется нативный constrained JSON output: Effect 3 + Vercel AI SDK.** Один `generateText` с `Output.object`, прямой Anthropic provider, без tools/agent loop; явно выбрать `structuredOutputMode: "outputFormat"` для совместимой модели. SDK формирует `output_config.format` и валидирует результат. Понадобится небольшая Effect-обёртка; Effect владеет deadline, отменой и политикой повторов. Effect 4 RC ради одного классификатора не оправдан; если основной проект независимо выбирает Effect 4, его встроенный AI становится кандидатом после проверок ниже. `effect-agent` здесь не нужен. [Vercel provider][v-provider], [Output][v-core], [Effect 4 adapter][e4-provider]

## Версии и зрелость

| Вариант | Проверенный npm snapshot | Следствие |
|---|---|---|
| Effect 3 | `effect@latest = 3.22.1`; `@effect/ai = 0.37.0`; `@effect/ai-anthropic = 0.27.0` | Стабильная major ядра, но AI-пакеты остаются `0.x`. Peer адаптера: Effect `^3.22.0`, AI `^0.37.0`, platform `^0.97.0`, experimental `^0.61.0`; у AI дополнительно rpc `^0.76.0`. |
| Effect 4 | `effect@rc` и `@effect/ai-anthropic@rc = 4.0.0-rc.112` | Prerelease. Core AI импортируется из `effect/unstable/ai`, HTTP — `effect/unstable/http`; отдельный `@effect/ai` не имеет RC tag. Адаптер требует Effect `^4.0.0-rc.112`; это не взаимозаменяемый апгрейд Effect 3. |
| Vercel | `ai@latest = 7.0.93`, `@ai-sdk/anthropic@latest = 4.0.49` | Stable dist-tags; не следует проектировать по старым примерам AI SDK 5/6. Оба пакета объявляют peer Zod `^3.25.76 || ^4.1.8`, даже если прикладная схема — Effect. |
| Effect Agent | `effect-agent@latest = 0.0.1-beta.3`, `@beta = 0.1.0-beta.57`; те же tags у `@effect-agent/core` | Активная линия — beta, peer Effect `^4.0.0-rc.112`. README прямо допускает изменения API и сохранённых данных до 1.0. |

Источники таблицы: [effect npm][n-effect], [AI npm][n-ai], [Anthropic Effect npm][n-eanth], [Vercel ai npm][n-vai], [Vercel Anthropic npm][n-vanth], [effect-agent npm][n-agent], [core npm][n-agentcore], [README на опубликованном gitHead][agent]. Dist-tags — снимок, не обещание совместимости; фиксировать проверенный набор lockfile.

## Что реально происходит со structured output

**Effect 3.** В `@effect/ai-anthropic@0.27.0`, `src/AnthropicLanguageModel.ts:324–350`, JSON response format превращается в один синтетический tool с `input_schema`; `tool_choice` принудительно выбирает его, `disable_parallel_tool_use: true`. `strict: true` и нативный `output_config.format` этим путём не добавляются. Затем `tool_use.input` сериализуется; сопровождающий текст для JSON-режима игнорируется. В `@effect/ai@0.37.0`, `src/LanguageModel.ts:1059–1083`, результат проходит `Schema.decode(Schema.parseJson(ResultSchema))`; отсутствие объекта и нарушение схемы дают `AiError.MalformedOutput`. Без toolkit этот путь не исполняет пользовательские инструменты и не требует агентного цикла. [Адаптер][e3-provider], [LanguageModel][e3-ai]

**Effect 4 RC.** В `@effect/ai-anthropic@4.0.0-rc.112` поддерживаемые модели получают `output_config.format = { type: "json_schema", schema }`. Есть `structuredOutputs` override и отдельный `strictJsonSchema` для инструментов — это разные настройки. Core использует `Schema.Codec` и provider codec transformer; локальное декодирование остаётся обязательным, ошибка структурированного результата — `AiError` с причиной `StructuredOutputError`, а неподдерживаемой схемы — `UnsupportedSchemaError`. Старый catch по `MalformedOutput` переносить буквально нельзя. [RC provider, `makeRequest`, `getOutputFormat`][e4-provider], [RC core, `CodecTransformer`, `generateObject`, `resolveStructuredOutput`][e4-core]

**Конкретный RC gate, вывод из кода:** `prepareTools` в опубликованном RC (строки 1371–1376) возвращает пустые tools **до** ветки fallback, если `options.tools.length === 0` или `toolChoice === "none"`. Именно так core инициализирует `generateObject` без toolkit (строки 881–887). При `structuredOutputs: false` нативный output тоже отключён. Следовательно, заявленный tool fallback для нашего простого вызова выглядит недостижимым. Это статически выявленная проблема, не воспроизведённый тест и не утверждение о всех будущих версиях. Нужен mock HTTP regression test перед выбором RC/legacy-модели. [RC provider][e4-provider], [RC core][e4-core]

**Vercel.** В `@ai-sdk/anthropic@4.0.49`, `src/anthropic-language-model.ts`, `structuredOutputMode` по умолчанию `auto`: native output при распознанной поддержке, иначе синтетический `json` tool; явный `outputFormat` выбирает native. Перед отправкой применяется `sanitizeJsonSchema`. В `ai@7.0.93`, `src/generate-text/output.ts`, `Output.object` выполняет JSON parse и `safeValidateTypes`; ошибки дают `NoObjectGeneratedError`, содержащий cause, text, response, usage, finishReason. Это не просто просьба вернуть JSON в prompt. [Provider][v-provider], [Core][v-core]

**Граница гарантий Anthropic.** Native JSON output — `output_config.format`; strict tools — `strict: true`. Forced tool choice сам по себе не равен strict. Поддерживается ограниченное подмножество JSON Schema: например, числовые min/max и длины строк нельзя автоматически считать ограничениями декодера. Refusal и обрезание по `max_tokens` могут нарушить схему даже при HTTP 200. Локальная валидация обязательна; структурная корректность не доказывает правильность spam verdict. [Официальная документация Anthropic][anthropic]

## Схема, ошибки, retry и cancellation

- **Единая схема.** Для Vercel на Effect 3 использовать `JSONSchema.make(SpamVerdict)` через `jsonSchema(..., { validate })`, где `validate` вызывает Effect Schema decoder и возвращает SDK `ValidationResult`. Схема DTO должна быть простой, без зависимых от сервисов преобразований; wire JSON Schema и локальные ограничения проверяются отдельно. `jsonSchema<T>(...)` без `validate` лишь объявляет тип и передаёт схему провайдеру. Одного `Schema.standardSchemaV1` недостаточно: Vercel требует также `~standard.jsonSchema` converter, иначе выбрасывает ошибку. [provider-utils `src/schema.ts`][v-utils], [Effect 3 Schema/JSONSchema][e3-core]
- **Отмена Promise.** Обёртка должна создавать вызов лениво внутри `Effect.tryPromise({ try: signal => generateText({ ..., abortSignal: signal }), catch: mapError })`. Адаптер Vercel передаёт signal HTTP-функции. Простой `tryPromise(() => alreadyStartedPromise)` не соединяет отмену fiber с запросом. В Effect AI используется Effect HTTP client: в Effect 3 `HttpClient.make` вызывает `AbortController.abort()` при interruption, обработка response body также interruptible; RC Fetch client передаёт signal в fetch. Это отмена клиентского ожидания/HTTP, а не доказательство прекращения вычисления и биллинга на сервере. [Effect core][e3-core], [Vercel provider][v-provider], [Effect platform][e3-http], [RC HTTP][e4-core]
- **Один владелец retry.** Vercel по умолчанию делает до двух повторов, exponential backoff; учитывает разумные `Retry-After`/`retry-after-ms` (до 60 секунд). Повторяет retryable API/Gateway errors; parsing/validation результата находится вне этой транспортной retry-обёртки. Для общего управления из Effect выставить `maxRetries: 0`; иначе внешний `Effect.retry` умножает число запросов. В проверенном Effect 3 `AnthropicClient.make/createMessage` встроенной retry-политики нет; custom HTTP client может её добавлять. [Vercel core, `prepare-retries.ts`, `retry-with-exponential-backoff.ts`, `generate-text.ts`][v-core], [Effect client][e3-provider]
- **Предлагаемая политика приложения:** один исходный запрос + максимум два повтора только transient transport/429/5xx, с jitter, учётом Retry-After и общим deadline поверх всей последовательности. Не повторять автоматически credentials/400/schema incompatibility/refusal; malformed output или token limit — отдельное ограниченное решение, не бесконечный repair loop. Сохранить cause/status/finishReason/usage; нормализовать в доменные `ClassifierTransportError`, `ClassifierInvalidOutput`, `ClassifierRefusal`, `ClassifierTimeout`. Interruption не превращать в обычный retryable failure. Конкретные теги приложения здесь — предложение, не API SDK.

## Почему без agent framework

`effect-agent` строит loop, исполняет tools, проверяет результат, предлагает bounded execution, streaming и durable runtime; использует Effect AI providers. Это полезно для многошагового агента, но не добавляет необходимой возможности единственному `generateObject`/`Output.object`. Для классификатора это дополнительный beta/RC dependency surface. Предлагаемый контракт: `classify(input): Effect<SpamVerdict, ClassifierError>`; решение о бане остаётся вне модели. [Официальный README, commit `a5657c9`][agent]

## Нерешённые gates перед реализацией

1. Зафиксировать точную Anthropic model ID и endpoint; подтвердить forced tool output для основного варианта или native structured output при выборе Vercel. Capability heuristics SDK не заменяют контракт провайдера. Не выбирать модель только по имени или примеру из документации.
2. На mock fetch проверить фактический payload: отсутствие инструментов модерации, forced schema tool либо native format согласно выбранному адаптеру, сериализованную схему и полный local decode; случаи bad JSON, missing/extra fields, refusal, token truncation. Для RC отдельно воспроизвести fallback issue выше.
3. Проверить отмену во время HTTP/body/backoff и общий deadline; посчитать фактические попытки при 429/5xx, проверить отсутствие повторов после interruption. Установить latency/token budget и правила для malformed/refusal.
4. Проверить совместимость JSONSchema export с SDK draft-07 и Anthropic subset, особенно refinements/nullable/extra fields; не предполагать, что sanitizer сохраняет все ограничения. Проверить сборкой полный peer set и требуемую версию runtime.
5. На размеченной выборке определить качество/стоимость/порог принятия решения. Без model calls в этом исследовании нет данных о precision, latency или цене одного классифицирования; schema validation этого не устанавливает.

Все gates остаются открытыми: исследование проверяет исходники, не выдаёт их чтение за runtime validation.

[n-effect]: https://registry.npmjs.org/effect
[n-ai]: https://registry.npmjs.org/@effect%2fai
[n-eanth]: https://registry.npmjs.org/@effect%2fai-anthropic
[n-vai]: https://registry.npmjs.org/ai
[n-vanth]: https://registry.npmjs.org/@ai-sdk%2fanthropic
[n-agent]: https://registry.npmjs.org/effect-agent
[n-agentcore]: https://registry.npmjs.org/@effect-agent%2fcore
[e3-provider]: https://registry.npmjs.org/@effect/ai-anthropic/-/ai-anthropic-0.27.0.tgz
[e3-ai]: https://registry.npmjs.org/@effect/ai/-/ai-0.37.0.tgz
[e3-core]: https://registry.npmjs.org/effect/-/effect-3.22.1.tgz
[e3-http]: https://registry.npmjs.org/@effect/platform/-/platform-0.97.0.tgz
[e4-provider]: https://registry.npmjs.org/@effect/ai-anthropic/-/ai-anthropic-4.0.0-rc.112.tgz
[e4-core]: https://registry.npmjs.org/effect/-/effect-4.0.0-rc.112.tgz
[v-provider]: https://registry.npmjs.org/@ai-sdk/anthropic/-/anthropic-4.0.49.tgz
[v-core]: https://registry.npmjs.org/ai/-/ai-7.0.93.tgz
[v-utils]: https://registry.npmjs.org/@ai-sdk/provider-utils/-/provider-utils-5.0.36.tgz
[anthropic]: https://platform.claude.com/docs/en/build-with-claude/structured-outputs
[agent]: https://github.com/danieljvdm/effect-agent/blob/a5657c976f34572b2fd5d1a9ef38c66c42724342/README.md
