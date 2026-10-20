# telegent для Android

Первый APK открывает существующий веб-интерфейс сервера в Android WebView.
Сверху находится строка `Telegent - for agents and humans`. Нужны Android 8.0+
и актуальный Android System WebView. Фоновых уведомлений в версии 0.1.3 нет.

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

## Сборка

Используются только платформенные Android API, стандартная библиотека Python и
официальные инструменты SDK/JDK; Gradle и сторонние библиотеки не нужны.
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
Сборка выполняет AAPT2 → javac → D8 → zipalign → apksigner и проверяет подпись.
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
0.1.3 has no background notifications. Server authorization is unchanged.
The login page and Settings link to the latest official APK. Starting with 0.1.1,
the app checks and downloads updates automatically, verifying the checksum, package,
version and signing key. Android confirms installation. Version 0.1.0 needs one manual
update to acquire this feature. Login and settings survive updates.
Version 0.1.2 checks on every opening and return to the foreground, without a six-hour
delay. Once downloaded, the update is offered while the app is visible. Deferred
updates are offered on the next visit without downloading the same APK again.

Build from the repository root using the commands above, JDK 21, Android SDK
platform 36 and Build Tools 36.0.0. The APK and checksum are in `android/build/`.
Keep a private backup of `android/.signing/` for future updates; never publish it.
The JVM checks do not replace testing login, messaging, attachments and lifecycle
on an Android device.

## Публикация выпуска

После проверок закоммитьте и отправьте исходники. Соберите APK этой версии с прежним
ключом, затем подготовьте публичные файлы:

```text
python tools/prepare_release.py --commit HEAD --apk android/build/telegent-0.1.3.apk --out android/build/release
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
