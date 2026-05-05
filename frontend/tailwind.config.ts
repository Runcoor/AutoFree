import type { Config } from 'tailwindcss'

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: ['selector', '[data-theme="dark"]'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', '"Noto Sans SC"', '-apple-system', 'BlinkMacSystemFont', '"Segoe UI"', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      colors: {
        // Token-mapped colors so light/dark switch via CSS vars
        bg: 'var(--bg)',
        'bg-elev': 'var(--bg-elev)',
        'bg-soft': 'var(--bg-soft)',
        line: 'var(--border)',
        'line-strong': 'var(--border-strong)',
        ink: {
          DEFAULT: 'var(--text)',
          soft: 'var(--text-muted)',
          muted: 'var(--text-muted)',
          faint: 'var(--text-faint)',
        },
        brand: {
          1: '#0072ff',
          2: '#00c6ff',
        },
        success: '#10b981',
        warning: '#f59e0b',
        warn: '#f59e0b',
        danger: '#ef4444',
        info: '#00c6ff',
      },
      borderRadius: {
        card: '16px',
        btn: '10px',
        input: '10px',
      },
      boxShadow: {
        sm: 'var(--shadow-sm)',
        md: 'var(--shadow-md)',
        lg: 'var(--shadow-lg)',
        glow: '0 6px 16px rgba(0,114,255,0.30)',
        'glow-lg': '0 10px 24px rgba(0,114,255,0.45)',
      },
      backgroundImage: {
        'brand-grad': 'linear-gradient(135deg, #0072ff 0%, #00c6ff 100%)',
        'brand-grad-soft': 'linear-gradient(135deg, rgba(0,114,255,0.10) 0%, rgba(0,198,255,0.10) 100%)',
      },
      transitionTimingFunction: {
        apple: 'cubic-bezier(0.4, 0, 0.2, 1)',
      },
      keyframes: {
        fadeInUp: {
          '0%': { opacity: '0', transform: 'translateY(12px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        pageIn: {
          '0%': { opacity: '0', transform: 'translateY(8px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
        shimmer: {
          '0%': { transform: 'translateX(-100%)' },
          '100%': { transform: 'translateX(100%)' },
        },
        shine: {
          '0%, 60%': { transform: 'translateX(-100%)' },
          '100%': { transform: 'translateX(100%)' },
        },
        float: {
          '0%, 100%': { transform: 'translate(0,0) scale(1)' },
          '50%': { transform: 'translate(60px, 40px) scale(1.1)' },
        },
        pulseDot: {
          '0%, 100%': { transform: 'scale(1)', opacity: '1' },
          '50%': { transform: 'scale(1.5)', opacity: '0' },
        },
      },
      animation: {
        'fade-in-up': 'fadeInUp 0.5s ease both',
        'page-in': 'pageIn 0.45s cubic-bezier(0.4,0,0.2,1) both',
        shimmer: 'shimmer 1.6s linear infinite',
        shine: 'shine 3s ease-in-out infinite',
        float: 'float 24s ease-in-out infinite',
        'pulse-dot': 'pulseDot 1.6s ease-out infinite',
      },
    },
  },
  plugins: [],
} satisfies Config
