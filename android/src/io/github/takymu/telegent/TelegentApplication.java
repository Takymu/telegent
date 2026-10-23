package io.github.takymu.telegent;

public final class TelegentApplication extends android.app.Application {
    @Override public void onCreate(){super.onCreate();NativePush.restore(this);}
}
