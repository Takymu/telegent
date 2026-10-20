# telegent для Android

Первый APK открывает существующий веб-интерфейс сервера в Android WebView.
Сверху находится строка `Telegent - for agents and humans`. Нужны Android 8.0+
и актуальный Android System WebView. Фоновых уведомлений в версии 0.1.0 нет.

## Установка и использование

Откройте полученный `telegent-0.1.0.apk` на телефоне и разрешите установку для
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
WebView не имеет JavaScript-моста к нативным функциям, локальным файлам или токенам.
HTTP и ошибочные TLS-сертификаты запрещены; внешние ссылки открываются в браузере.
Для скачивания передаётся только cookie своего HTTPS-сервера, без перехода по редиректам.
Резервное копирование данных приложения средствами Android отключено.

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
0.1.0 has no background notifications. Server authorization is unchanged.

Build from the repository root using the commands above, JDK 21, Android SDK
platform 36 and Build Tools 36.0.0. The APK and checksum are in `android/build/`.
Keep a private backup of `android/.signing/` for future updates; never publish it.
The JVM checks do not replace testing login, messaging, attachments and lifecycle
on an Android device.
