package io.github.takymu.telegent;

public final class PushPolicyTest {
    private static void check(boolean value){if(!value)throw new AssertionError();}
    public static void main(String[] args){
        String server="1234567890abcdef1234567890abcdef";
        check(PushPolicy.accepts(server,server,"42"));
        for(String id:new String[]{"0","-1","42?token=foo","1/2","9007199254740992","NaN"})check(!PushPolicy.accepts(server,server,id));
        check(!PushPolicy.accepts(server,"different","42"));
        check(PushPolicy.command("telegent-push://toggle")[0].equals("toggle"));
        check(PushPolicy.command("telegent-push://result/"+server+"/ok")[2].equals("ok"));
        for(String url:new String[]{"https://example.org/toggle","telegent-push://toggle?server=evil","telegent-push://user@toggle", "telegent-push://toggle#token=secret", "telegent-push://result/bad/ok", "telegent-push://opened/../42"})check(PushPolicy.command(url)==null);
        System.out.println("Push policy checks passed");
    }
}
