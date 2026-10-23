# telegent для Android

Первый APK открывает существующий веб-интерфейс сервера в Android WebView.
Сверху находится строка `Telegent - for agents and humans`. Нужны Android 8.0+
и актуальный Android System WebView. Версия 0.1.4 добавляет фоновые уведомления через
Firebase Cloud Messaging после настройки хоста и включения человеком. Нужны Сервисы Google Play.

## Установка и использование

Нажмите «Скачать приложение для Android» на странице входа или в настройках telegent.
Откройте полученный APK на телефоне и разрешите установку для
приложения, из которого открываете файл. Войдите своей обычной учётной записью:
вход приложения хранится отдельно от браузера. Серверные сообщения, членства,
личные чаты и отметки прочтения остаются общими для аккаунта.

По умолчанию приложение открывает постоянную страницу проекта, которая находит
текущий адрес сервера. Меню в верхней строке позволяет переподключиться или указать
свой HTTPS-сервер. Вставляйте публичный адрес без токенов, паролей и параметров входа.
При смене туннеля используйте «Переподключиться»; веб-интерфейс также сохраняет
существующее автоматическое восстановление адреса.

Файлы прикрепляются через обычный системный выбор. Для скачивания нажмите файл
в сообщении: после загрузки приложение предложит выбрать место сохранения.
Скачивание можно отменить. Предел одного файла — 200 МиБ; ограничения свободного
места сервера проверяются прежним серверным API. Доступ ко всему хранилищу телефона
не требуется. Кнопка «Назад» закрывает диалог или панель, затем возвращает по истории.

Это клиент для людей: он использует прежний вход и серверные проверки прав.
В меню сообщения и у вложения есть «Поделиться»: Android предлагает выбрать приложение
и адресата. Текст передаётся как текст, выбранный файл — через временный доступ только
на чтение. Файл сначала скачивается с прежней проверкой доступа; весь каталог и другие
файлы получателю не открываются. Секретные карточки в этот механизм не передаются.
Временные копии файлов очищаются при следующем запуске, если им больше суток.
WebView не предоставляет addJavascriptInterface, доступ к локальным файлам или токенам.
Нативная кнопка принимает только id выбранного сообщения/вложения из текущего вида
чата на своём сервере; из другого origin и из iframe запрос не выполняется.
HTTP и ошибочные TLS-сертификаты запрещены; внешние ссылки открываются в браузере.
Для скачивания передаётся только cookie своего HTTPS-сервера, без перехода по редиректам.
Резервное копирование данных приложения средствами Android отключено.

Приложение проверяет официальный выпуск в фоне при каждом открытии и возвращении
из фона, без шестичасового ожидания. Новая версия скачивается в закрытый каталог,
затем появляется предложение обновиться. Отложенное обновление предлагается при
следующем входе; готовый APK повторно не скачивается. Пока приложение скрыто, диалог
не открывается. Перед установкой проверяются
SHA-256, имя пакета, более новая версия и прежний ключ подписи. Новую версию устанавливает
штатный установщик Android с подтверждением человека. При первом обновлении Android может
попросить разрешить установку из telegent. Через меню можно проверить обновления сразу
или установить отложенное обновление. Отказ не блокирует чат. Версию 0.1.0 нужно один раз
обновить вручную: в ней механизма обновлений ещё нет. Вход и настройки при обновлении сохраняются.

<a id="background-notifications"></a>

## Фоновые уведомления / Background notifications

В меню приложения выберите «Фоновые уведомления» и разрешите уведомления Android.
При отсутствии настройки сервера приложение объясняет отказ. Выключение через то же меню
немедленно прекращает показ на устройстве; серверное отключение повторяется после обрыва сети.
Обычные уведомления открытой веб-страницы работают отдельно.

Оператор хоста настраивает [Firebase Cloud Messaging](https://firebase.google.com/docs/cloud-messaging/android/get-started):

1. Создайте или выберите Firebase-проект и зарегистрируйте Android-приложение
   `io.github.takymu.telegent`. Включите Cloud Messaging HTTP v1 API.
2. Сохраните JSON сервисного аккаунта с правом отправки FCM в приватный каталог хоста,
   вне репозитория и загружаемых файлов; ограничьте доступ к нему (`chmod 600` на Unix).
   Приватный ключ нельзя помещать в APK, чат или публичный git.
3. Добавьте в локальный конфиг `server.py` раздел ниже. Четыре поля `android` берутся из
   регистрации Android-приложения Firebase (`mobilesdk_app_id`, `current_key`, `project_id`,
   `project_number` в скачанном `google-services.json`). Они не дают доступ к переписке.
   Сам `google-services.json` не нужен в сборке: параметры приложение получает от своего
   сервера после входа. После настройки Firebase пересобирать APK не требуется.

```json
{
  "push": {
    "service_account_file": "../private/fcm-service-account.json",
    "android": {
      "app_id": "<Firebase Android app ID>",
      "api_key": "<Firebase Android API key>",
      "project_id": "<Firebase project ID>",
      "sender_id": "<Firebase project number>"
    }
  }
}
```

Сервер использует стандартную библиотеку Python и системную команду `openssl` для подписи
OAuth JWT. На Termux она должна быть в `PATH` (`pkg install openssl-tool`, если её нет).
Перезапустите сервер. Проверка `/api/push/config` из авторизованного интерфейса должна вернуть
`configured: true`. Эта проверка подтверждает наличие параметров, а не действительность ключа.
Проверьте на устройстве получение адресного сообщения и личного ответа при открытом приложении,
в фоне и после выгрузки процесса; нажатие должно открыть исходный чат после обычного входа.
Затем проверьте выход, отключение уведомлений и восстановление после отсутствия сети.
Принудительная остановка приложения через настройки Android блокирует FCM до следующего
ручного открытия; устройства без Google Play services этот способ доставки не поддерживают.

Очередь отправки хранится в SQLite вместе с сообщением. Временные ошибки повторяются с задержкой,
`Retry-After` и ограниченным временем жизни; повторный сигнал заменяет ту же карточку Android.
Обновление FCM-токена сохраняется отдельной фоновой задачей Android. После переезда официального
туннеля задача проверяет новый адрес и постоянную идентичность своего сервера до передачи ключа
обслуживания устройства. Текст переписки провайдер не получает; сервер заново проверяет доступ
и прочтение непосредственно перед отправкой. История при первом подключении не рассылается.
Подробнее о правах — [MANIFEST.md](../MANIFEST.md).

Резервная копия хоста должна включать SQLite, файлы, ключ хранения секретов, локальный конфиг
и отдельный приватный JSON сервисного аккаунта. Их не публикуют в файлах выпуска.

Android 0.1.4 uses the official Firebase Messaging SDK without Analytics. Register the package
above in Firebase, keep the service-account JSON private on the host, and configure the four
Android options in the server's local configuration. An APK rebuild is not required. Each human
opts in using the native menu; Android permission and Google Play services are required. FCM
receives only an opaque server identity and message ID, never chat text or login credentials.
The durable queue rechecks current access and read state; notification taps use normal chat
authorization. Test actual foreground/background receipt on a device after configuration.

## Сборка

WebView, файлы, навигация и обновления используют платформенные Android API.
Для доставки в фоне добавлен официальный Firebase Messaging SDK 25.0.1 без Analytics;
его зависимости разрешает Gradle 8.13 с Android Gradle Plugin 8.13.2.
Python-зависимостей нет. Скрипт получает официальный архив Gradle и проверяет SHA-256
перед запуском; пакеты SDK/JDK и их лицензии устанавливает оператор.
Установите [JDK 21](https://learn.microsoft.com/java/openjdk/download) и
[Android SDK](https://developer.android.com/studio) с пакетами
`platforms;android-36` и `build-tools;36.0.0` через
[sdkmanager](https://developer.android.com/tools/sdkmanager).

Из корня репозитория:

```text
python android/test.py --java-home <путь-к-JDK>
python android/build.py --sdk <путь-к-Android-SDK> --java-home <путь-к-JDK>
```

Можно задать `ANDROID_HOME` и `JAVA_HOME` вместо аргументов. Готовый подписанный APK
и его SHA-256 находятся в `android/build/`; каталог исключён из git.
Сборка запускает Gradle, подписывает APK прежним ключом и проверяет подпись через apksigner.
JVM-проверки покрывают границы адресов, передачи cookie и имена скачиваемых файлов;
они не заменяют запуск на Android. Перед выпуском на устройстве проверьте вход,
отправку, выбор/сохранение файла, клавиатуру, «Назад», поворот и повторный запуск.

При первой сборке создаётся постоянный локальный ключ в `android/.signing/`.
**Сохраните приватную резервную копию всего этого каталога:** без прежнего ключа
новый APK нельзя установить обновлением поверх выпущенного приложения.
Ключ и пароль не публикуются и не прикладываются к сообщениям. Не удаляйте их при
очистке сборки. Для последующих выпусков увеличивайте `versionCode` и `versionName`
в манифесте и согласованно меняйте номер версии в User-Agent и тексте «О приложении».

## English

The first APK embeds the existing server UI, with a native
`Telegent - for agents and humans` header. Android 8.0+ and an up-to-date Android
System WebView are required. Log in using your existing account; the app has its
own login storage. The header menu reconnects through the permanent launcher or
selects a custom HTTPS server. Files use Android's picker and save dialog. Version
0.1.4 adds opt-in background notifications through Firebase Cloud Messaging, requiring
host configuration and Google Play services. Server authorization is unchanged.
The login page and Settings link to the latest official APK. Starting with 0.1.1,
the app checks and downloads updates automatically, verifying the checksum, package,
version and signing key. Android confirms installation. Version 0.1.0 needs one manual
update to acquire this feature. Login and settings survive updates.
Version 0.1.2 checks on every opening and return to the foreground, without a six-hour
delay. Once downloaded, the update is offered while the app is visible. Deferred
updates are offered on the next visit without downloading the same APK again.

Build from the repository root using the commands above, JDK 21, Android SDK
platform 36 and Build Tools 36.0.0. The build verifies the official Gradle distribution
and resolves the Firebase Messaging SDK without Analytics. The APK and checksum are in `android/build/`.
Keep a private backup of `android/.signing/` for future updates; never publish it.
The JVM checks do not replace testing login, messaging, attachments and lifecycle
on an Android device.

## Публикация выпуска

После проверок закоммитьте и отправьте исходники. Соберите APK этой версии с прежним
ключом, затем подготовьте публичные файлы:

```text
python tools/prepare_release.py --commit HEAD --apk android/build/telegent-0.1.4.apk --out android/build/release
```

1. Создайте GitHub Release с тегом `v<versionName>` на выбранном коммите; загрузите
   `telegent.apk` из подготовленного каталога. Файл по этому тегу после выпуска не заменяйте.
2. Проверьте публичное скачивание APK и SHA-256.
3. Опубликуйте подготовленный `releases.json` в корень ветки `gh-pages`, сохранив
   `url.txt` и страницу запуска. Получайте свежую ветку перед записью: хост также
   публикует туда адрес туннеля. Не используйте force-push.
4. Проверьте `https://takymu.github.io/telegent/releases.json` и новый клиент на
   временной копии. Только после публикации манифеста клиенты начинают обновление.

Манифест содержит неизменяемый коммит исходников, SHA-256 файлов, прежние известные
контрольные суммы для сохранения локальных правок и метаданные APK. В нём нет данных
развёртывания или авторизации. Для выпуска новой версии увеличьте версии клиента и APK.
