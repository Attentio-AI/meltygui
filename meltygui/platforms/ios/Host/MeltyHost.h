#pragma once
#import <Foundation/Foundation.h>
#import <QuartzCore/QuartzCore.h>
#import "InputQueue.hpp"

@interface MeltyHost : NSObject
- (instancetype)initWithLayer:(CAMetalLayer *)layer
                     viewport:(CALayer *)viewport
                       status:(void (^)(NSString *))status
                     keyboard:(void (^)(BOOL))keyboard
                     safeZone:(void (^)(CGFloat))safeZone;
- (void)start;
- (void)setActive:(BOOL)active;
- (void)close;
- (void)resizeTo:(CGSize)size scale:(CGFloat)scale;
- (void)enqueue:(melty::InputEvent)event;
- (void)requestFrame;
- (void)setKeyboardVisible:(BOOL)visible;
- (void)setSafeZone:(CGFloat)inset;
- (NSString *)clipboardText;
- (void)setClipboardText:(NSString *)text;
- (void)writeLog:(NSString *)text;
@end
