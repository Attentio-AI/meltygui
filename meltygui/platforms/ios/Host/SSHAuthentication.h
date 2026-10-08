#pragma once
#import <Security/Security.h>
#import <UniformTypeIdentifiers/UniformTypeIdentifiers.h>

static NSMutableDictionary *sshKeychainQuery(NSString *account) {
    return [@{(__bridge id)kSecClass: (__bridge id)kSecClassGenericPassword,
              (__bridge id)kSecAttrService: @"melty.ssh",
              (__bridge id)kSecAttrAccount: account} mutableCopy];
}

static UIViewController *sshPresentationController() {
    for (UIScene *scene in UIApplication.sharedApplication.connectedScenes) {
        if (![scene isKindOfClass:UIWindowScene.class] || scene.activationState != UISceneActivationStateForegroundActive) continue;
        for (UIWindow *window in ((UIWindowScene *)scene).windows) {
            if (!window.isKeyWindow) continue;
            UIViewController *controller = window.rootViewController;
            while (controller.presentedViewController) controller = controller.presentedViewController;
            return controller;
        }
    }
    return nil;
}

@interface MeltySSHCredentials : NSObject <UIDocumentPickerDelegate>
@property(atomic, copy) NSString *status;
@property(atomic, copy) NSString *username;
@property(atomic, copy) NSString *error;
@property(nonatomic, copy) NSString *host;
@property(nonatomic) NSInteger port;
@property(nonatomic, copy) NSString *kind;
@property(nonatomic, copy) NSString *privateKey;
@property(nonatomic, strong) UIViewController *dialog;
- (void)start;
- (void)cancel;
@end

@implementation MeltySSHCredentials
- (void)cancel {
    self.status = @"cancelled";
    self.privateKey = nil;
    [self.dialog dismissViewControllerAnimated:YES completion:nil];
    self.dialog = nil;
}
- (void)finishError:(NSString *)message {
    self.error = message;
    self.privateKey = nil;
    self.status = @"error";
    self.dialog = nil;
}
- (void)showLogin {
    UIViewController *parent = sshPresentationController();
    if (!parent) { [self finishError:@"No active window for SSH authentication."]; return; }
    UIAlertController *alert = [UIAlertController alertControllerWithTitle:self.host
        message:nil preferredStyle:UIAlertControllerStyleAlert];
    [alert addTextFieldWithConfigurationHandler:^(UITextField *field) {
        field.placeholder = @"Username";
        field.text = self.username;
        field.enabled = !self.username.length;
        field.autocapitalizationType = UITextAutocapitalizationTypeNone;
        field.autocorrectionType = UITextAutocorrectionTypeNo;
        field.textContentType = UITextContentTypeUsername;
    }];
    [alert addTextFieldWithConfigurationHandler:^(UITextField *field) {
        field.placeholder = [self.kind isEqualToString:@"key"] ? @"Key passphrase (optional)" : @"Password";
        field.secureTextEntry = YES;
        field.textContentType = UITextContentTypePassword;
    }];
    __weak MeltySSHCredentials *weak = self;
    [alert addAction:[UIAlertAction actionWithTitle:@"Cancel" style:UIAlertActionStyleCancel handler:^(UIAlertAction *action) {
        [weak cancel];
    }]];
    [alert addAction:[UIAlertAction actionWithTitle:@"Connect" style:UIAlertActionStyleDefault handler:^(UIAlertAction *action) {
        MeltySSHCredentials *owner = weak;
        if (!owner) return;
        UIAlertController *form = (UIAlertController *)owner.dialog;
        NSString *username = [form.textFields[0].text stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
        if (!username.length || [username rangeOfCharacterFromSet:[NSCharacterSet characterSetWithCharactersInString:@" /@?#:\r\n\t"]].location != NSNotFound) {
            [owner finishError:@"Enter a valid SSH username."];
            return;
        }
        NSString *secret = form.textFields[1].text ?: @"";
        NSDictionary *values = [owner.kind isEqualToString:@"key"]
            ? @{@"username": username, @"private_key": owner.privateKey ?: @"", @"passphrase": secret}
            : @{@"username": username, @"password": secret};
        NSData *data = [NSJSONSerialization dataWithJSONObject:values options:0 error:nil];
        // Match the root's identity, including a host-only root. Its login name
        // belongs to these credentials; changing it must not orphan open files.
        NSString *account = [NSString stringWithFormat:@"ssh:%@:%ld:%@", owner.host, (long)owner.port, owner.username];
        NSMutableDictionary *query = sshKeychainQuery(account);
        NSDictionary *attributes = @{(__bridge id)kSecValueData: data,
            (__bridge id)kSecAttrAccessible: (__bridge id)kSecAttrAccessibleWhenUnlockedThisDeviceOnly};
        OSStatus result = SecItemUpdate((__bridge CFDictionaryRef)query, (__bridge CFDictionaryRef)attributes);
        if (result == errSecItemNotFound) {
            [query addEntriesFromDictionary:attributes];
            result = SecItemAdd((__bridge CFDictionaryRef)query, nullptr);
        }
        form.textFields[1].text = @"";
        owner.privateKey = nil;
        owner.dialog = nil;
        if (result != errSecSuccess) { [owner finishError:@"Could not save SSH credentials in Keychain."]; return; }
        owner.username = username;
        owner.status = @"saved";
    }]];
    self.dialog = alert;
    [parent presentViewController:alert animated:YES completion:nil];
}
- (void)start {
    if (![self.kind isEqualToString:@"key"]) { [self showLogin]; return; }
    UIViewController *parent = sshPresentationController();
    if (!parent) { [self finishError:@"No active window for key import."]; return; }
    UIDocumentPickerViewController *picker = [[UIDocumentPickerViewController alloc]
        initForOpeningContentTypes:@[UTTypeData] asCopy:NO];
    picker.delegate = self;
    self.dialog = picker;
    [parent presentViewController:picker animated:YES completion:nil];
}
- (void)documentPickerWasCancelled:(UIDocumentPickerViewController *)controller { [self cancel]; }
- (void)documentPicker:(UIDocumentPickerViewController *)controller didPickDocumentsAtURLs:(NSArray<NSURL *> *)urls {
    NSURL *url = urls.firstObject;
    if (!url) { [self cancel]; return; }
    BOOL scoped = [url startAccessingSecurityScopedResource];
    NSNumber *size = nil;
    [url getResourceValue:&size forKey:NSURLFileSizeKey error:nil];
    NSData *data = size && size.unsignedLongLongValue <= 262144
        ? [NSData dataWithContentsOfURL:url options:0 error:nil] : nil;
    if (scoped) [url stopAccessingSecurityScopedResource];
    NSString *text = data ? [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding] : nil;
    if (![text containsString:@"PRIVATE KEY-----"]) {
        [self finishError:@"Choose an OpenSSH or PEM private key (up to 256 KB)."];
        return;
    }
    self.privateKey = text;
    // Finish dismissing the document picker before presenting the login form.
    [controller dismissViewControllerAnimated:YES completion:^{
        self.dialog = nil;
        if ([self.status isEqualToString:@"pending"]) [self showLogin];
    }];
}
@end

static const char *sshCredentialsCapsule = "melty.ssh-credentials";
static void releaseSSHCredentials(PyObject *capsule) {
    void *pointer = PyCapsule_GetPointer(capsule, sshCredentialsCapsule);
    if (!pointer) return;
    MeltySSHCredentials *prompt = CFBridgingRelease(pointer);
    dispatch_async(dispatch_get_main_queue(), ^{ if ([prompt.status isEqualToString:@"pending"]) [prompt cancel]; });
}
static PyObject *configureSSH(PyObject *, PyObject *args) {
    const char *host, *user, *kind;
    int port;
    if (!PyArg_ParseTuple(args, "siss", &host, &port, &user, &kind)) return nullptr;
    MeltySSHCredentials *prompt = [MeltySSHCredentials new];
    prompt.host = [NSString stringWithUTF8String:host];
    prompt.port = port;
    prompt.username = [NSString stringWithUTF8String:user];
    prompt.kind = [NSString stringWithUTF8String:kind];
    prompt.status = @"pending";
    void *owned = (__bridge_retained void *)prompt;
    PyObject *capsule = PyCapsule_New(owned, sshCredentialsCapsule, releaseSSHCredentials);
    if (!capsule) { CFBridgingRelease(owned); return nullptr; }
    dispatch_async(dispatch_get_main_queue(), ^{ [prompt start]; });
    return capsule;
}
static PyObject *sshConfigurationStatus(PyObject *, PyObject *capsule) {
    void *pointer = PyCapsule_GetPointer(capsule, sshCredentialsCapsule);
    if (!pointer) return nullptr;
    MeltySSHCredentials *prompt = (__bridge MeltySSHCredentials *)pointer;
    return Py_BuildValue("{s:s,s:s,s:s}", "status", prompt.status.UTF8String,
                         "username", prompt.username.UTF8String ?: "", "error", prompt.error.UTF8String ?: "");
}
static PyObject *sshCredentials(PyObject *, PyObject *args) {
    const char *account;
    if (!PyArg_ParseTuple(args, "s", &account)) return nullptr;
    NSMutableDictionary *query = sshKeychainQuery([NSString stringWithUTF8String:account]);
    query[(__bridge id)kSecReturnData] = @YES;
    query[(__bridge id)kSecMatchLimit] = (__bridge id)kSecMatchLimitOne;
    CFTypeRef found = nullptr;
    OSStatus status = SecItemCopyMatching((__bridge CFDictionaryRef)query, &found);
    if (status == errSecItemNotFound) Py_RETURN_NONE;
    if (status != errSecSuccess) { PyErr_SetString(PyExc_OSError, "SSH Keychain is unavailable. Unlock the device and retry."); return nullptr; }
    NSData *data = CFBridgingRelease(found);
    return PyUnicode_DecodeUTF8(static_cast<const char *>(data.bytes), data.length, "strict");
}
