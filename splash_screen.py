#!/usr/bin/env python3
"""
CEC-Sonos Bridge - TV Splash Screen
Displays a splash screen on the TV with QR code to sonosbridge.local

Uses framebuffer to display directly on HDMI output.
"""

import os
import sys
import subprocess
import logging

log = logging.getLogger(__name__)

APP_DIR = '/opt/cec-sonos-bridge'
SPLASH_IMAGE = f'{APP_DIR}/splash.png'
SPLASH_URL = 'http://sonosbridge.local'


def generate_splash_image():
    """Generate splash screen PNG with QR code."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        import qrcode
    except ImportError:
        log.warning("PIL or qrcode not installed, skipping splash generation")
        return False
    
    # Screen dimensions (1920x1080 for full HD)
    width, height = 1920, 1080
    
    # Colors
    bg_color = (26, 26, 46)  # Dark blue background
    text_color = (255, 255, 255)  # White text
    accent_color = (0, 212, 170)  # Teal accent
    
    # Create image
    img = Image.new('RGB', (width, height), bg_color)
    draw = ImageDraw.Draw(img)
    
    # Generate QR code
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=2
    )
    qr.add_data(SPLASH_URL)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color=accent_color, back_color=bg_color)
    qr_img = qr_img.resize((300, 300))
    
    # Try to load fonts, fall back to default
    try:
        font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 72)
        font_medium = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 36)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
    except:
        font_large = ImageFont.load_default()
        font_medium = ImageFont.load_default()
        font_small = ImageFont.load_default()
    
    # Draw speaker icon (circle with lines)
    cx, cy = width // 2, 150
    draw.ellipse([cx - 40, cy - 40, cx + 40, cy + 40], fill=accent_color)
    draw.polygon([(cx - 20, cy - 20), (cx - 20, cy + 20), (cx + 10, cy + 35), (cx + 10, cy - 35)], fill=bg_color)
    
    # Draw title
    draw.text((width // 2, 280), "Sonos Bridge", font=font_large, fill=text_color, anchor="mm")
    
    # Draw subtitle
    draw.text((width // 2, 360), "Control your Sonos with your TV remote", font=font_medium, fill=(180, 180, 180), anchor="mm")
    
    # Draw status indicator
    draw.ellipse([width//2 - 10, 420, width//2 + 10, 440], fill=(40, 167, 69))  # Green dot
    draw.text((width // 2 + 25, 430), "Active", font=font_small, fill=(40, 167, 69), anchor="lm")
    
    # Paste QR code (centered below status)
    qr_x = (width - 300) // 2
    qr_y = 500
    img.paste(qr_img, (qr_x, qr_y))
    
    # Draw URL below QR
    draw.text((width // 2, 840), "sonosbridge.local", font=font_medium, fill=accent_color, anchor="mm")
    
    # Draw instruction
    draw.text((width // 2, 920), "Scan QR code or visit the URL above", font=font_small, fill=(150, 150, 150), anchor="mm")
    draw.text((width // 2, 960), "to access the admin panel", font=font_small, fill=(150, 150, 150), anchor="mm")
    
    # Draw bottom border accent
    draw.rectangle([0, height - 10, width, height], fill=accent_color)
    
    # Save image
    os.makedirs(APP_DIR, exist_ok=True)
    img.save(SPLASH_IMAGE, 'PNG')
    log.info(f"Splash image generated: {SPLASH_IMAGE}")
    return True


def display_splash():
    """Display splash image on framebuffer."""
    if not os.path.exists(SPLASH_IMAGE):
        if not generate_splash_image():
            return False
    
    # Try different methods to display the image
    
    # Method 1: fbi (framebuffer imageviewer)
    try:
        # Kill any existing fbi process
        subprocess.run(['killall', 'fbi'], capture_output=True)
        
        # Display image
        subprocess.Popen(
            ['fbi', '-T', '1', '-d', '/dev/fb0', '-noverbose', '-a', SPLASH_IMAGE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        log.info("Splash screen displayed via fbi")
        return True
    except Exception as e:
        log.warning(f"fbi method failed: {e}")
    
    # Method 2: Direct framebuffer write with fim
    try:
        subprocess.Popen(
            ['fim', '-d', '/dev/fb0', '-a', SPLASH_IMAGE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        log.info("Splash screen displayed via fim")
        return True
    except Exception as e:
        log.warning(f"fim method failed: {e}")
    
    # Method 3: Using pygame (if available)
    try:
        os.environ['SDL_VIDEODRIVER'] = 'fbcon'
        os.environ['SDL_FBDEV'] = '/dev/fb0'
        import pygame
        pygame.init()
        screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        image = pygame.image.load(SPLASH_IMAGE)
        image = pygame.transform.scale(image, screen.get_size())
        screen.blit(image, (0, 0))
        pygame.display.flip()
        log.info("Splash screen displayed via pygame")
        return True
    except Exception as e:
        log.warning(f"pygame method failed: {e}")
    
    log.warning("Could not display splash screen")
    return False


def clear_splash():
    """Clear the splash screen."""
    try:
        subprocess.run(['killall', 'fbi'], capture_output=True)
        subprocess.run(['killall', 'fim'], capture_output=True)
        # Clear console
        subprocess.run(['clear'], shell=True)
    except:
        pass


def main():
    """Generate and display splash screen."""
    logging.basicConfig(level=logging.INFO)
    
    if len(sys.argv) > 1:
        if sys.argv[1] == 'generate':
            generate_splash_image()
        elif sys.argv[1] == 'display':
            display_splash()
        elif sys.argv[1] == 'clear':
            clear_splash()
    else:
        generate_splash_image()
        display_splash()


if __name__ == '__main__':
    main()
