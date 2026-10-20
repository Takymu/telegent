package io.github.takymu.telegent;

import java.net.URI;
import java.util.Locale;

/** The release channel is independent of messages and custom chat servers. */
public final class UpdatePolicy {
    public static final String FEED = "https://takymu.github.io/telegent/releases.json";
    public static boolean asset(String value) {
        try {
            URI uri = new URI(value);
            return "https://github.com".equals(NavigationPolicy.origin(value))
                && uri.getRawQuery() == null && uri.getRawFragment() == null
                && uri.getRawPath().matches("/Takymu/telegent/releases/download/v[0-9]+\\.[0-9]+\\.[0-9]+/telegent\\.apk");
        } catch (Exception error) { return false; }
    }
    public static boolean transport(String value) {
        try {
            URI uri = new URI(value); String host = uri.getHost().toLowerCase(Locale.ROOT);
            return NavigationPolicy.origin(value) != null && uri.getPort() == -1
                && (value.equals(FEED) || host.equals("github.com") || host.equals("release-assets.githubusercontent.com")
                    || host.equals("objects.githubusercontent.com"));
        } catch (Exception error) { return false; }
    }
    public static boolean newer(long installed, long offered, long bytes, String digest, String url) {
        return offered > installed && bytes > 0 && bytes <= 200L*1024*1024
            && digest != null && digest.matches("[0-9a-f]{64}") && asset(url);
    }
}
