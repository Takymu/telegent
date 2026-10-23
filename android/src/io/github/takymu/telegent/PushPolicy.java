package io.github.takymu.telegent;

import java.net.URI;

/** Push links contain commands and opaque identities, never destinations or credentials. */
public final class PushPolicy {
    public static String[] command(String value) {
        try {
            URI uri=new URI(value);String action=uri.getHost(),path=uri.getRawPath();
            if(!"telegent-push".equals(uri.getScheme())||uri.getRawUserInfo()!=null||uri.getPort()!=-1
                    ||uri.getRawQuery()!=null||uri.getRawFragment()!=null)return null;
            if(("toggle".equals(action)||"sync".equals(action)||"logout".equals(action))&&(path.isEmpty()||"/".equals(path)))return new String[]{action};
            if("result".equals(action)&&path.matches("/[0-9a-f]{32}/(ok|error)"))return new String[]{action,path.split("/")[1],path.split("/")[2]};
            if("opened".equals(action)&&messageId(path.substring(1))>0)return new String[]{action,path.substring(1)};
        }catch(Exception ignored){}
        return null;
    }

    public static long messageId(String value) {
        try { if(value==null||!value.matches("[1-9][0-9]{0,15}"))return 0;long id=Long.parseLong(value);return id<=9007199254740991L?id:0; }
        catch(NumberFormatException error){return 0;}
    }

    public static boolean accepts(String expected,String received,String message) {
        return expected!=null&&expected.matches("[0-9a-f]{32}")&&expected.equals(received)&&messageId(message)>0;
    }
}
