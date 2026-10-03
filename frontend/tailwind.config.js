/** @type {import('tailwindcss').Config} */
export default {
  theme: {
    extend: {
      colors: {
        // 主色：沿用 Tailwind 官方 sky 色系（sky blue）
        // 使用方式：bg-primary-500 / text-primary-700 / ring-primary-300 等
        primary: {
          DEFAULT: '#0ea5e9', // sky-500，bg-primary 不带数字时取这个值
          50: '#f0f9ff',
          100: '#e0f2fe',
          200: '#bae6fd',
          300: '#7dd3fc',
          400: '#38bdf8',
          500: '#0ea5e9',
          600: '#0284c7',
          700: '#0369a1',
          800: '#075985',
          900: '#0c4a6e',
          950: '#082f49',
        },
      },
    },
  },
}

// 说明：Tailwind v4 会自动扫描项目源文件生成工具类，
// 所以这里不需要再写 content 配置；本文件通过 src/index.css 里的 @config 指令加载。
