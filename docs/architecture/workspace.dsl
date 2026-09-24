workspace "Мультиагентная AI-платформа анализа и разрешения сервисных обращений" {

    !identifiers hierarchical

    model {
        # ---------- Пользователи ----------
        partnerUser = person "Представитель партнёра" {
            description "Подаёт обращение, видит только оборудование и обращения своего партнёра"
        }
        engineer = person "Инженер" {
            description "Получает диагноз и доступ к внутренним техническим документам своей территории"
        }
        dispatcher = person "Диспетчер" {
            description "Проверяет рекомендацию и подтверждает создание документа. Видит всё в своей территории"
        }
        serviceManager = person "Сервис-менеджер" {
            description "Согласование сверх порога, эскалации. Несколько территорий; обход блокирующих правил — target"
        }

        # ---------- Внешние системы ----------
        erp = softwareSystem "Учётная система" {
            description "SAP S/4HANA Service. Оборудование, партнёры, договоры, цены, история обслуживания, сервисные заказы"
            tags "External"
        }
        ticketing = softwareSystem "Система регистрации обращений" {
            description "Внешний канал поступления обращений и получатель результатов исполнения"
            tags "External"
        }
        dwh = softwareSystem "Хранилище данных" {
            description "Приёмник документов для отчётности"
            tags "External"
        }

        # ---------- Целевая система ----------
        platform = softwareSystem "AI-платформа сервисных обращений" {
            description "Анализ обращения, диагноз по документации, применение правил, подготовка контролируемого действия"

            ui = container "Интерфейс оператора" {
                description "Обращение, ход разбора, источники, кандидаты в исполнители, подтверждение"
                technology "Streamlit"
            }

            api = container "API и оркестратор" {
                description "Стейт-машина процесса, маршрутизация, персистентное состояние, прерывание на человеке, деградация"
                technology "Python, FastAPI, явный конечный автомат (ADR-0002)"

                fsm = component "Оркестратор / FSM" {
                    description "Координирует агентов и инструменты; Context и Knowledge параллельны. Route берёт из каталога; недостаток фактов ведёт к человеку"
                    technology "Python: backend/orchestrator/machine.py"
                }
                authz = component "Контекст авторизации" {
                    description "Определяет территории и уровни доступа пользователя, передаётся в каждый вызов"
                }
                stateManager = component "Менеджер состояния" {
                    description "Состояние, решения, execution grants и аудит в PostgreSQL; идемпотентность. Автоматический clarification/resume не реализован"
                }
                guardIn = component "Входной контроль" {
                    description "Персональные данные, попытки внедрения инструкций, отсечение запросов вне области"
                }
                safetyGate = component "Предохранитель безопасности" {
                    description "Признаки опасности прерывают процесс независимо от прочих результатов"
                }
                contextAgent = component "Context Agent" {
                    description "Извлечение идентификаторов оборудования и партнёра из свободного текста"
                    tags "Agent"
                }
                knowledgeAgent = component "Knowledge Agent" {
                    description "Сопоставление симптома с документацией, гипотеза причины со ссылками на источники"
                    tags "Agent"
                }
                policyAgent = component "Policy Agent" {
                    description "Применимость одного requires_evaluation: applicable / not_applicable / insufficient. Минимальный PolicyCase; без выбора route и транзакционных решений"
                    tags "Agent"
                }
                visionTool = component "Распознавание изображения" {
                    description "Извлечение модели, серийного номера и кода ошибки; результат сверяется со справочником"
                }
                routingTool = component "Исполнение и подбор исполнителя" {
                    description "fulfillment.decide: тип исполнения, центр и исполнитель по территории, компетенции, авторизации и приоритетам договора/карточки. Route передаёт FSM"
                }
                warrantyTool = component "Предварительная квалификация гарантии" {
                    description "W-01: срок с даты продажи и risk flags; preliminary=true. Договор и расширенная гарантия не входят в расчёт MVP"
                }
                urgencyTool = component "Операционная срочность" {
                    description "Детерминированный расчёт по urgency_hint, влиянию и сроку реакции договора; не меняет route"
                }
                approvalTool = component "Уровень согласования" {
                    description "Сравнение суммы с порогом и с ценой изделия"
                }
                guardOut = component "Выходной контроль" {
                    description "Проверка существования артикулов, моделей и значения классификатора решения"
                }
                humanGate = component "Узел подтверждения человеком" {
                    description "Подтверждение/согласование человеком в методах FSM; сохранение execution grant до вызова исполнителя"
                }
                executor = component "Исполнитель действий" {
                    description "Проверка сохранённого execution grant и состояния; создание черновика с ключом идемпотентности; повтор с тем же ключом при сбое"
                }
            }

            rules = container "Механизм правил" {
                description "Детерминированные правила. Порядок применения задаётся типом условия: безопасность, конкретный код и модель, общий симптом. Числовой приоритет в разрешении конфликтов не участвует"
                technology "Python"
            }

            retrieval = container "Сервис поиска" {
                description "Гибридный поиск: плотное и разреженное представления, ранговое слияние, фильтр по правам"
                technology "Python, BM25"
            }

            ingestion = container "Конвейер загрузки документов" {
                description "Разбор документации, разбиение на фрагменты, разметка уровнем доступа, построение индексов"
                technology "Python; docling — target"
            }

            inference = container "Сервис инференса" {
                description "Языковая модель, модель обработки изображений, модель векторных представлений"
                technology "vLLM, Qwen3-8B AWQ, Qwen2.5-VL"
                tags "GPU"
            }

            adapter = container "Адаптер учётной системы" {
                description "ErpPort и SapODataAdapter; контекст авторизации обязателен. Live MVP использует custom SAP-like OData mock"
                technology "Python"
            }

            mockErp = container "Мок учётной системы" {
                description "Custom ZAI_SERVICE: OData V4-like subset над синтетическими данными; не released SAP standard API"
                technology "Python, FastAPI, PostgreSQL"
                tags "MVP"
            }

            stateDb = container "Хранилище состояния" {
                description "Обращения, шаги процесса, вызовы инструментов, согласования, аудит, пользователи и права"
                technology "PostgreSQL"
                tags "Database"
            }

            vectorDb = container "Векторное хранилище" {
                description "Фрагменты документов, плотные и разреженные представления, метаданные доступа"
                technology "Qdrant"
                tags "Database"
            }

            observability = container "Наблюдаемость" {
                description "Prometheus/Grafana и Langfuse развёрнуты; сквозная OpenTelemetry/LLM-трассировка приложения — target"
                technology "OpenTelemetry, Langfuse, Prometheus, Grafana"
            }
        }

        # ---------- Связи уровня контекста ----------
        partnerUser -> platform "Подаёт обращение, получает статус и рекомендацию"
        engineer -> platform "Получает диагноз и внутренние технические документы"
        dispatcher -> platform "Проверяет рекомендацию, подтверждает создание документа"
        serviceManager -> platform "Согласовывает сверх порога; обход блокировок — target"

        platform -> erp "Запрашивает факты, создаёт черновик сервисного заказа"
        ticketing -> platform "Передаёт обращения"
        platform -> ticketing "Возвращает результат разбора"
        platform -> dwh "Передаёт документы для отчётности"

        # ---------- Связи уровня контейнеров ----------
        partnerUser -> platform.ui "Подаёт обращение"
        dispatcher -> platform.ui "Проверяет и подтверждает"
        engineer -> platform.ui "Смотрит диагноз"
        serviceManager -> platform.ui "Согласовывает"

        platform.ui -> platform.api "Вызывает" "HTTP в MVP; HTTPS — target"
        ticketing -> platform.api "Передаёт обращения" "HTTPS"

        platform.api -> platform.rules "Применяет детерминированные правила"
        platform.api -> platform.retrieval "Ищет фрагменты документации с учётом прав"
        platform.api -> platform.inference "Запрашивает рассуждение и распознавание" "OpenAI-совместимый API"
        platform.api -> platform.adapter "Получает факты, создаёт документы"
        platform.api -> platform.stateDb "Сохраняет состояние, согласования, аудит"
        platform.api -> platform.observability "Трассировка и метрики"

        platform.retrieval -> platform.vectorDb "Гибридный запрос с фильтром по правам"
        platform.retrieval -> platform.inference "Векторное представление запроса"
        platform.ingestion -> platform.vectorDb "Записывает фрагменты и индексы"
        platform.ingestion -> platform.inference "Векторные представления фрагментов"

        platform.adapter -> platform.mockErp "Рабочий путь MVP: custom ZAI_SERVICE" "OData V4-like / HTTP внутри Docker-сети"
        platform.adapter -> erp "Целевая реализация" "OData"

        platform.mockErp -> platform.observability "Журналы"
        platform.inference -> platform.observability "Метрики инференса"

        # ---------- Связи уровня компонентов ----------
        # Стрелки обозначают вызовы/зависимости, а не передачу управления между агентами.
        platform.ui -> platform.api.authz "Передаёт пользователя"
        platform.api.authz -> platform.api.fsm "Проверенный контекст авторизации"
        platform.api.fsm -> platform.api.guardIn "Контроль до вызова моделей"
        platform.api.fsm -> platform.api.safetyGate "Проверка опасности; при признаках эскалация"
        platform.api.safetyGate -> platform.rules "Слова предохранителя из каталога"

        platform.api.fsm -> platform.api.contextAgent "Идентификация; параллельно с Knowledge"
        platform.api.fsm -> platform.api.knowledgeAgent "Поиск и гипотеза; параллельно с Context"
        platform.api.contextAgent -> platform.inference "Извлечение фактов из текста"
        platform.api.contextAgent -> platform.api.visionTool "Распознать табличку при наличии фото"
        platform.api.visionTool -> platform.inference "Обработка изображения"
        platform.api.contextAgent -> platform.adapter "Идентификация и проверка фактов через ErpPort"
        platform.api.knowledgeAgent -> platform.retrieval "Гибридный поиск с учётом прав"
        platform.api.knowledgeAgent -> platform.inference "Гипотеза со ссылками на источники"

        platform.api.fsm -> platform.rules "Детерминированная оценка; route и действия из каталога"
        platform.api.fsm -> platform.api.policyAgent "Только одно requires_evaluation; минимальный PolicyCase"
        platform.api.policyAgent -> platform.inference "Оценка применимости; результат возвращается FSM"

        platform.api.fsm -> platform.api.warrantyTool "Предварительная гарантия по проверенным фактам"
        platform.api.fsm -> platform.adapter "Читает центры, исполнителей, запчасти; проверяет факты"
        platform.api.fsm -> platform.api.routingTool "Передаёт route из каталога и данные ERP"
        platform.api.fsm -> platform.api.urgencyTool "Вычисляет срочность отдельно от route"
        platform.api.fsm -> platform.api.approvalTool "Определяет требуемый уровень согласования"
        platform.api.fsm -> platform.api.guardOut "Проверяет rule, route, центр, исполнителя и артикулы"
        platform.api.fsm -> platform.api.humanGate "После выходного контроля ждёт решение человека"
        platform.ui -> platform.api.humanGate "Подтвердить / отклонить / согласовать через API"
        platform.api.humanGate -> platform.api.stateManager "Сохраняет решение человека и execution grant"
        platform.api.fsm -> platform.api.executor "Исполнение только после сохранённого разрешения"
        platform.api.executor -> platform.api.stateManager "Проверяет grant; сохраняет результат и аудит"
        platform.api.executor -> platform.adapter "Создаёт черновик с ключом идемпотентности"
        platform.api.fsm -> platform.api.stateManager "Состояние, шаги и запись вызовов"
        platform.api.stateManager -> platform.stateDb "Состояние, аудит, идемпотентность"

        # ---------- Развёртывание MVP ----------
        deploymentEnvironment "MVP" {
            deploymentNode "Арендованный GPU-узел" {
                description "MVP GPU node, Selectel, GPU класса 48 ГБ; runtime 49 140 МиБ, Ubuntu 22.04"
                technology "Docker Compose"

                deploymentNode "Приложение и логические модули" {
                    description "Rules, retrieval и adapter исполняются внутри API; ingestion — отдельная команда. Не один Docker-контейнер на каждый блок"
                    containerInstance platform.ui
                    containerInstance platform.api
                    containerInstance platform.rules
                    containerInstance platform.retrieval
                    containerInstance platform.ingestion
                    containerInstance platform.adapter
                    containerInstance platform.mockErp
                    containerInstance platform.observability
                }
                deploymentNode "Хранилища" {
                    containerInstance platform.stateDb
                    containerInstance platform.vectorDb
                }
                deploymentNode "GPU" {
                    description "48 ГБ видеопамяти"
                    containerInstance platform.inference
                }
            }
        }

        # ---------- Целевое развёртывание ----------
        deploymentEnvironment "Целевое" {
            deploymentNode "Внутренний периметр" {
                deploymentNode "Узел приложений" {
                    description "Горизонтально масштабируемый"
                    containerInstance platform.ui
                    containerInstance platform.api
                    containerInstance platform.rules
                    containerInstance platform.retrieval
                    containerInstance platform.adapter
                }
                deploymentNode "Слой хранилищ" {
                    containerInstance platform.stateDb
                    containerInstance platform.vectorDb
                }
                deploymentNode "Узел инференса" {
                    description "Выделенный узел с GPU, отдельная сетевая зона"
                    containerInstance platform.inference
                }
                deploymentNode "Узел наблюдаемости" {
                    containerInstance platform.observability
                }
            }
        }
    }

    views {
        systemContext platform "C1_Context" {
            description "Корпоративный контекст; SAP, ticketing и DWH — целевые интеграции. MVP работает с custom SAP-like mock"
            include *
            autolayout lr 400 250
        }

        container platform "C2_Containers" {
            description "Логическая декомпозиция; Rules, retrieval и adapter в MVP исполняются внутри API. Внешние интеграции — target"
            include *
            autolayout lr 400 250
        }

        component platform.api "C3_Orchestrator" {
            description "Внутреннее устройство оркестратора: агенты, инструменты, контроль, исполнение"
            include *
            autolayout lr 600 300
        }

        deployment platform "MVP" "Deployment_MVP" {
            description "Единый узел: обосновано стоимостью и сроком реализации"
            include *
            autolayout lr 400 250
        }

        deployment platform "Целевое" "Deployment_Target" {
            description "Разделение узла приложений, слоя хранилищ и узла инференса"
            include *
            autolayout lr 400 250
        }

        styles {
            element "Person" {
                shape person
                background #1168bd
                color #ffffff
            }
            element "Software System" {
                background #1168bd
                color #ffffff
            }
            element "External" {
                background #999999
                color #ffffff
            }
            element "Container" {
                background #438dd5
                color #ffffff
            }
            element "Component" {
                background #85bbf0
                color #000000
            }
            element "Agent" {
                background #f5a623
                color #000000
            }
            element "Database" {
                shape cylinder
            }
            element "GPU" {
                background #d0021b
                color #ffffff
            }
            element "MVP" {
                background #7ed321
                color #000000
            }
        }

    }
}
