// Send one iMessage for icloud-mac-helper. Static script, one JSON argument: {"guid": "<chat guid>"} for an existing conversation,
// or {"handle": "<email or +number>"} to start one, plus "text". Only iMessage: a conversation that exists only as SMS, or a handle
// without iMessage, is refused rather than sent another way. The chat or participant is resolved BEFORE sending and an unknown one
// throws, so a message can never go to a different conversation than the one named.
// A 1:1 conversation comes with both guid and handle (its one participant). Messages scripts only the conversations it currently
// shows, so when it does not know the guid (-1728) the message goes to that participant over iMessage instead; a group never does.
function run(argv) {
  var a = JSON.parse(argv[0]);
  var Messages = Application("Messages");
  if (a.guid) {
    var chat = Messages.chats.byId(a.guid);
    try {
      chat.id();                                                    // throws (-1728) if Messages does not show this chat
    } catch (e) {
      if (!a.handle || !(e.errorNumber === -1728 || String(e).indexOf("-1728") >= 0)) throw e;
      chat = null;
    }
    if (chat) {
      Messages.send(a.text, {to: chat});
      return "sent";
    }
  }
  var account = null;
  Messages.accounts().forEach(function (x) {
    try { if (!account && String(x.serviceType()) === "iMessage" && x.enabled()) account = x; } catch (e) {}
  });
  if (!account) throw new Error("no enabled iMessage account on this Mac");
  var who = account.participants.byName(a.handle);
  who.handle();                                                     // throws if iMessage does not know this handle
  Messages.send(a.text, {to: who});
  return "sent";
}
