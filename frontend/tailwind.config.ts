import type { Config } from 'tailwindcss'

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      fontFamily: {
        sans: [
          '-apple-system',
          'BlinkMacSystemFont',
          '"SF Pro Text"',
          '"SF Pro Display"',
          '"Helvetica Neue"',
          '"PingFang SC"',
          '"Microsoft YaHei"',
          'sans-serif',
        ],
      },
      colors: {
        bg: '#F5F5F7',
        surface: '#FFFFFF',
        ink: {
          DEFAULT: '#1D1D1F',
          soft: '#6E6E73',
          muted: '#86868B',
        },
        accent: {
          DEFAULT: '#007AFF',
          hover: '#0066CC',
          subtle: 'rgba(0, 122, 255, 0.08)',
        },
        danger: '#FF3B30',
        warning: '#FF9500',
        success: '#34C759',
        line: 'rgba(0, 0, 0, 0.06)',
      },
      borderRadius: {
        card: '16px',
        btn: '12px',
        input: '10px',
      },
      boxShadow: {
        sm: '0 1px 3px rgba(0, 0, 0, 0.04)',
        md: '0 4px 24px rgba(0, 0, 0, 0.06)',
        lg: '0 12px 48px rgba(0, 0, 0, 0.10)',
      },
      fontSize: {
        'display': ['36px', { lineHeight: '1.15', letterSpacing: '-0.02em', fontWeight: '700' }],
        'title': ['22px', { lineHeight: '1.25', letterSpacing: '-0.01em', fontWeight: '600' }],
        'body': ['17px', { lineHeight: '1.5' }],
        'caption': ['13px', { lineHeight: '1.4', color: 'rgba(0,0,0,.55)' }],
      },
      transitionTimingFunction: {
        'apple': 'cubic-bezier(.25,.1,.25,1)',
      },
    },
  },
  plugins: [],
} satisfies Config
