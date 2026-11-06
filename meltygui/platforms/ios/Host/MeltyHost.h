#pragma once
#import <Foundation/Foundation.h>
#import <QuartzCore/QuartzCore.h>
#import "InputQueue.hpp"

@interface MeltyHost : NSObject
- (instancetype)initWithLayer:(CAMetalLayer *)layer
                     viewport:(CALayer *)viewport
                       status:(void (^)(NSString *))status
                     keyboard:(void (^)(BOOL))keyboard
                     safeZone:(void (^)(CGFloat, CGFloat))safeZone;
@property(nonatomic, copy) void (^windowTabsChanged)(NSArray<NSString *> *);
- (void)setWindowTabs:(NSArray<NSString *> *)titles;
- (void)selectWindowTab:(NSInteger)index;
- (void)start;
- (void)setActive:(BOOL)active;
- (void)close;
- (void)resizeTo:(CGSize)size scale:(CGFloat)scale;
- (void)enqueue:(melty::InputEvent)event;
- (void)enqueueBatch:(std::vector<melty::InputEvent>)events;
- (void)requestFrame;
- (void)setKeyboardVisible:(BOOL)visible;
- (void)setSafeZone:(CGFloat)top bottom:(CGFloat)bottom;
- (void)setFPSVisible:(BOOL)visible;
- (NSString *)clipboardText;
- (void)setClipboardText:(NSString *)text;
- (void)writeLog:(NSString *)text;
@end
