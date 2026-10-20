package io.github.takymu.telegent;

/** Real JVM checks; no Android stubs or test libraries are executed. */
public final class NavigationPolicyTest {
    private static int checks;
    private static void check(boolean result, String message) {
        checks++;
        if (!result) throw new AssertionError(message);
    }
    public static void main(String[] args) {
        check("https://chat.example".equals(NavigationPolicy.origin("https://CHAT.example:443/path#secret")), "normalize HTTPS origin");
        check("https://chat.example:8443".equals(NavigationPolicy.origin("https://chat.example:8443/")), "custom HTTPS port");
        for (String value : new String[]{"http://chat.example/", "javascript:alert(1)", "file:///data/private", "content://private", "https://user:password@chat.example/", "https://chat.example:0/", "https://chat.example:65536/", null})
            check(NavigationPolicy.origin(value) == null, "reject non-HTTPS or credential-bearing address");
        check(NavigationPolicy.sameOrigin("https://chat.example/a", "https://chat.example:443/b"), "same origin across paths");
        check(!NavigationPolicy.sameOrigin("https://chat.example.evil.test/", "https://chat.example/"), "suffix is not same origin");
        check(!NavigationPolicy.sameOrigin("https://chat.example:8443/", "https://chat.example/"), "port separates origins");
        check(NavigationPolicy.launcher(NavigationPolicy.DEFAULT_ENTRY+"#token=fixture", NavigationPolicy.DEFAULT_ENTRY), "launcher token handoff stays in known path");
        check(!NavigationPolicy.launcher("https://takymu.github.io/unrelated/", NavigationPolicy.DEFAULT_ENTRY), "other launcher paths are external");
        check(NavigationPolicy.discoveredServer("https://fixture.trycloudflare.com/#token=fixture"), "accept official tunnel handoff");
        check(!NavigationPolicy.discoveredServer("https://fixture.trycloudflare.com.evil.test/"), "reject fake tunnel suffix");
        check(!NavigationPolicy.discoveredServer("https://fixture.trycloudflare.com/external"), "discover only a server root");
        check(!NavigationPolicy.discoveredServer("http://fixture.trycloudflare.com/"), "never discover cleartext");
        check(NavigationPolicy.entry(" https://chat.example/ ").equals("https://chat.example/"), "trim entry address");
        check(NavigationPolicy.entry("https://chat.example/#token=fixture") == null, "never persist token fragment as configuration");
        check(NavigationPolicy.entry("https://chat.example/?password=fixture") == null, "never persist secret query as configuration");
        check(NavigationPolicy.attachment("https://chat.example/api/attachments/42", "https://chat.example/"), "allow authorized attachment path");
        for (String value : new String[]{"https://other.example/api/attachments/42", "https://chat.example:8443/api/attachments/42", "https://chat.example/api/attachments/42/extra", "https://chat.example/api/attachments/42#token=fixture", "https://chat.example/api/attachments/../secret"})
            check(!NavigationPolicy.attachment(value, "https://chat.example/"), "never forward a download cookie outside its origin and path");
        check("https://chat.example/".equals(NavigationPolicy.publicUrl("https://chat.example/?secret=fixture#token=fixture")), "browser handoff strips private query and fragment");
        check("отчёт+1.txt".equals(NavigationPolicy.downloadName("attachment; filename*=UTF-8''%D0%BE%D1%82%D1%87%D1%91%D1%82+1.txt", "file")), "preserve Unicode and literal plus in server file names");
        check("fallback.txt".equals(NavigationPolicy.downloadName("attachment; filename*=UTF-8''bad%ZZ", "fallback.txt")), "malformed encoding keeps fallback");
        check(".._private_file.txt".equals(NavigationPolicy.downloadName("attachment; filename*=UTF-8''..%2Fprivate%5Cfile.txt", "file")), "file name cannot supply a path");
        check("file".equals(NavigationPolicy.downloadName(null, "..")), "reject special path name");
        String longName = new String(new char[150]).replace('\0', 'я');
        check(NavigationPolicy.downloadName(null, longName).getBytes(java.nio.charset.StandardCharsets.UTF_8).length <= 200, "bound Unicode name by UTF-8 byte length");
        String asset="https://github.com/Takymu/telegent/releases/download/v0.1.1/telegent.apk";
        check(UpdatePolicy.newer(1,2,33000,new String(new char[64]).replace('\0','a'),asset),"valid newer official APK");
        check(!UpdatePolicy.newer(2,2,33000,new String(new char[64]).replace('\0','a'),asset),"never reinstall current APK");
        check(!UpdatePolicy.newer(2,1,33000,new String(new char[64]).replace('\0','a'),asset),"never downgrade");
        for(String url:new String[]{"http://github.com/Takymu/telegent/releases/download/v0.1.1/telegent.apk",asset+"?token=fixture",asset.replace("Takymu","other"),"https://chat.example/update.apk"})
            check(!UpdatePolicy.asset(url),"release cannot redirect the source repository");
        check(UpdatePolicy.transport("https://release-assets.githubusercontent.com/asset?signature=fixture"),"official HTTPS asset redirect");
        check(!UpdatePolicy.transport("https://release-assets.githubusercontent.com.evil.test/asset"),"fake asset host rejected");
        check(!UpdatePolicy.transport("http://release-assets.githubusercontent.com/asset"),"cleartext release rejected");
        check("message".equals(NavigationPolicy.shareRequest("telegent-share://message/42")[0]),"share a selected message ID");
        check("42".equals(NavigationPolicy.shareRequest("telegent-share://attachment/42")[1]),"share a selected attachment ID");
        for(String uri:new String[]{"telegent-share://message/0","telegent-share://message/-1","telegent-share://other/42",
                "telegent-share://message/42?text=private","telegent-share://message/42#private","telegent-share://message/../42",
                "telegent-share://message:443/42","telegent-share://user@message/42","https://message/42","telegent-share://attachment/%34%32"})
            check(NavigationPolicy.shareRequest(uri)==null,"share route cannot carry text, credentials or arbitrary paths");
        System.out.println("Navigation checks passed: " + checks);
    }
}
