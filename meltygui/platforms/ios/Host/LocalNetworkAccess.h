#pragma once
#import <Network/Network.h>
#include <dns_sd.h>
#include <memory>

// One short-lived Bonjour round trip per request. Browser readiness alone is
// not proof of permission: wait until it discovers this listener's unique name.
struct LocalNetworkAccess {
    std::atomic<int> status{0}; // pending, granted, denied, failed
    nw_browser_t browser = nil;
    nw_listener_t listener = nil;
    void stop() { // main queue only
        if (browser) nw_browser_cancel(browser);
        if (listener) nw_listener_cancel(listener);
        browser = nil;
        listener = nil;
    }
};
using LocalNetworkRequest = std::shared_ptr<LocalNetworkAccess>;
static const char *localNetworkCapsule = "melty.local-network-access";

static void releaseLocalNetworkAccess(PyObject *capsule) {
    auto *owned = static_cast<LocalNetworkRequest *>(PyCapsule_GetPointer(capsule, localNetworkCapsule));
    if (!owned) return;
    auto request = *owned;
    dispatch_async(dispatch_get_main_queue(), ^{ request->stop(); });
    delete owned;
}

static PyObject *requestLocalNetworkAccess(PyObject *, PyObject *) {
    auto request = std::make_shared<LocalNetworkAccess>();
    auto *owned = new LocalNetworkRequest(request);
    PyObject *capsule = PyCapsule_New(owned, localNetworkCapsule, releaseLocalNetworkAccess);
    if (!capsule) { delete owned; return nullptr; }
    dispatch_async(dispatch_get_main_queue(), ^{
        const char *type = "_melty-access._tcp";
        NSString *name = NSUUID.UUID.UUIDString;
        std::weak_ptr<LocalNetworkAccess> weak = request;
        auto parameters = nw_parameters_create_secure_tcp(NW_PARAMETERS_DISABLE_PROTOCOL,
                                                           NW_PARAMETERS_DEFAULT_CONFIGURATION);
        request->listener = nw_listener_create(parameters);
        auto descriptor = nw_browse_descriptor_create_bonjour_service(type, "local.");
        request->browser = nw_browser_create(descriptor, parameters);
        if (!request->listener || !request->browser) {
            request->status = 3;
            request->stop();
            return;
        }
        nw_listener_set_advertise_descriptor(request->listener,
            nw_advertise_descriptor_create_bonjour_service(name.UTF8String, type, "local."));
        nw_listener_set_new_connection_handler(request->listener, ^(nw_connection_t connection) {
            nw_connection_cancel(connection); // No application traffic is accepted.
        });
        auto reportError = ^(nw_error_t error) {
            if (auto state = weak.lock()) {
                state->status = error && nw_error_get_error_domain(error) == nw_error_domain_dns &&
                    nw_error_get_error_code(error) == kDNSServiceErr_PolicyDenied ? 2 : 3;
            }
        };
        nw_listener_set_state_changed_handler(request->listener, ^(nw_listener_state_t state, nw_error_t error) {
            if (state == nw_listener_state_waiting || state == nw_listener_state_failed) reportError(error);
        });
        nw_browser_set_state_changed_handler(request->browser, ^(nw_browser_state_t state, nw_error_t error) {
            if (state == nw_browser_state_waiting || state == nw_browser_state_failed) reportError(error);
        });
        nw_browser_set_browse_results_changed_handler(request->browser,
            ^(nw_browse_result_t oldResult, nw_browse_result_t result, bool batchComplete) {
                if (!result) return;
                nw_endpoint_t endpoint = nw_browse_result_copy_endpoint(result);
                const char *found = nw_endpoint_get_bonjour_service_name(endpoint);
                if (found && [name isEqualToString:[NSString stringWithUTF8String:found]]) {
                    if (auto state = weak.lock()) state->status = 1;
                }
            });
        nw_listener_set_queue(request->listener, dispatch_get_main_queue());
        nw_browser_set_queue(request->browser, dispatch_get_main_queue());
        nw_listener_start(request->listener);
        nw_browser_start(request->browser);
    });
    return capsule;
}

static PyObject *localNetworkAccessStatus(PyObject *, PyObject *capsule) {
    auto *request = static_cast<LocalNetworkRequest *>(PyCapsule_GetPointer(capsule, localNetworkCapsule));
    if (!request) return nullptr;
    const char *states[] = {"pending", "granted", "denied", "failed"};
    return PyUnicode_FromString(states[(*request)->status.load()]);
}

static PyObject *openAppSettings(PyObject *, PyObject *) {
    dispatch_async(dispatch_get_main_queue(), ^{
        [UIApplication.sharedApplication openURL:[NSURL URLWithString:UIApplicationOpenSettingsURLString]
                                         options:@{} completionHandler:nil];
    });
    Py_RETURN_NONE;
}
