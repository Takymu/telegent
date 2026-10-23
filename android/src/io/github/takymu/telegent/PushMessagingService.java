package io.github.takymu.telegent;

import android.app.Notification;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import com.google.firebase.messaging.FirebaseMessagingService;
import com.google.firebase.messaging.RemoteMessage;

public final class PushMessagingService extends FirebaseMessagingService {
    @Override public void onNewToken(String token){
        NativePush.preferences(this).edit().putString("fcm_token",token).putString("job","refresh").apply();
        PushRefreshService.schedule(this);
    }
    @Override public void onMessageReceived(RemoteMessage message){
        SharedPreferences prefs=NativePush.preferences(this);
        String id=message.getData().get("message_id"),server=message.getData().get("server_id");
        if(!prefs.getBoolean("wanted",false)||!prefs.getBoolean("registered",false)||!NativePush.permitted(this)
                ||!PushPolicy.accepts(prefs.getString("server_id",null),server,id))return;
        NativePush.channel(this);
        Intent intent=new Intent(this,MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP|Intent.FLAG_ACTIVITY_SINGLE_TOP)
            .setData(Uri.parse("telegent-notification://"+server+"/"+id))
            .putExtra("message_id",id).putExtra("server_id",server);
        PendingIntent open=PendingIntent.getActivity(this,id.hashCode(),intent,PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
        Notification notification=new Notification.Builder(this,NativePush.CHANNEL).setSmallIcon(R.drawable.notification_icon)
            .setContentTitle("telegent").setContentText(getString(R.string.push_new_message)).setContentIntent(open)
            .setAutoCancel(true).setVisibility(Notification.VISIBILITY_PRIVATE).setCategory(Notification.CATEGORY_MESSAGE)
            .setGroup("telegent").build();
        getSystemService(NotificationManager.class).notify("telegent-"+server+"-"+id,0,notification);
    }
}
