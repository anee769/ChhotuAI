---
name: Refined Industrial
colors:
  surface: '#f7f9fb'
  surface-dim: '#d8dadc'
  surface-bright: '#f7f9fb'
  surface-container-lowest: '#ffffff'
  surface-container-low: '#f2f4f6'
  surface-container: '#eceef0'
  surface-container-high: '#e6e8ea'
  surface-container-highest: '#e0e3e5'
  on-surface: '#191c1e'
  on-surface-variant: '#45464d'
  inverse-surface: '#2d3133'
  inverse-on-surface: '#eff1f3'
  outline: '#76777d'
  outline-variant: '#c6c6cd'
  surface-tint: '#565e74'
  primary: '#000000'
  on-primary: '#ffffff'
  primary-container: '#131b2e'
  on-primary-container: '#7c839b'
  inverse-primary: '#bec6e0'
  secondary: '#006c49'
  on-secondary: '#ffffff'
  secondary-container: '#6cf8bb'
  on-secondary-container: '#00714d'
  tertiary: '#000000'
  on-tertiary: '#ffffff'
  tertiary-container: '#0b1c30'
  on-tertiary-container: '#75859d'
  error: '#ba1a1a'
  on-error: '#ffffff'
  error-container: '#ffdad6'
  on-error-container: '#93000a'
  primary-fixed: '#dae2fd'
  primary-fixed-dim: '#bec6e0'
  on-primary-fixed: '#131b2e'
  on-primary-fixed-variant: '#3f465c'
  secondary-fixed: '#6ffbbe'
  secondary-fixed-dim: '#4edea3'
  on-secondary-fixed: '#002113'
  on-secondary-fixed-variant: '#005236'
  tertiary-fixed: '#d3e4fe'
  tertiary-fixed-dim: '#b7c8e1'
  on-tertiary-fixed: '#0b1c30'
  on-tertiary-fixed-variant: '#38485d'
  background: '#f7f9fb'
  on-background: '#191c1e'
  surface-variant: '#e0e3e5'
typography:
  headline-xl:
    fontFamily: Inter
    fontSize: 48px
    fontWeight: '800'
    lineHeight: 56px
    letterSpacing: -0.02em
  headline-lg:
    fontFamily: Inter
    fontSize: 32px
    fontWeight: '700'
    lineHeight: 40px
    letterSpacing: -0.01em
  headline-lg-mobile:
    fontFamily: Inter
    fontSize: 24px
    fontWeight: '700'
    lineHeight: 32px
  headline-md:
    fontFamily: Inter
    fontSize: 24px
    fontWeight: '600'
    lineHeight: 32px
  body-lg:
    fontFamily: Inter
    fontSize: 18px
    fontWeight: '400'
    lineHeight: 28px
  body-md:
    fontFamily: Inter
    fontSize: 16px
    fontWeight: '400'
    lineHeight: 24px
  label-md:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: '600'
    lineHeight: 20px
    letterSpacing: 0.01em
  label-sm:
    fontFamily: Inter
    fontSize: 12px
    fontWeight: '500'
    lineHeight: 16px
    letterSpacing: 0.02em
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  margin-page: 2rem
  gutter-grid: 1.5rem
  stack-xl: 3rem
  stack-md: 1.5rem
  stack-sm: 0.75rem
  inset-component: 1rem 1.25rem
---

## Brand & Style
The brand personality is authoritative yet approachable, shifting from a heavy industrial aesthetic to a polished, high-end SaaS experience. It targets professional operators and analysts who require precision and reliability but value a modern, fluid interface.

The design style is a hybrid of **Corporate Modern** and **Glassmorphism**. It utilizes the structural integrity of industrial layouts but softens them with translucent layers, ambient shadows, and increased negative space. The goal is to evoke a sense of "technical calm"—a high-performance tool that feels effortless to use.

## Colors
The palette is anchored by a deep **Industrial Navy** (#0F172A) for primary branding and navigation. The functional star is the **Minty Action Green** (#10B981), used exclusively for "Safe" states, success indicators, and primary calls to action. 

To bridge these, a sophisticated range of **Zinc and Slate neutrals** provides background layering and secondary text contrast. Subtle linear gradients (180°) are applied to primary buttons and header surfaces, transitioning from the base color to a slightly lighter tint (5-10% lighter) to create a metallic, premium sheen without looking dated.

## Typography
This design system utilizes **Inter** across all roles to maintain a systematic, utilitarian feel, but leverages varied weights and tight tracking to create distinction. 

Headlines use Bold and ExtraBold weights with slight negative letter-spacing to appear "compact" and engineered. Body text is kept at a generous 16px base for readability, while labels use Medium or SemiBold weights in uppercase for clear information hierarchy in data-heavy views.

## Layout & Spacing
The layout follows a **Fluid Grid** model with a maximum content width of 1440px. We have increased the default gutter to 1.5rem (24px) to ensure the interface feels "airy" and allows the eyes to rest between data clusters.

- **Desktop (1024px+):** 12-column grid, 32px page margins.
- **Tablet (768px - 1023px):** 8-column grid, 24px page margins.
- **Mobile (<767px):** 4-column grid, 16px page margins.

Component padding has been expanded to a minimum of 1rem vertically to move away from the dense, cramped feeling of traditional ledger software.

## Elevation & Depth
Visual hierarchy is established through **Tonal Layers** and **Ambient Shadows**. Instead of harsh 1px borders, we use soft shadows to lift components off the background.

1.  **Level 0 (Background):** Slate-50 (#F8FAFC) - the base canvas.
2.  **Level 1 (Cards/Surface):** White with a 4px blur, 2% opacity shadow.
3.  **Level 2 (Modals/Dropdowns):** White with a 12px blur, 8% opacity shadow.

**Glassmorphism** is applied to sticky navigation bars and floating sidebars using a `backdrop-filter: blur(12px)` and a semi-transparent white stroke (10% opacity) to create a "frosted" look that maintains the context of the content underneath.

## Shapes
The design system transitions to a **Rounded** (Level 2) language. The base 0.5rem (8px) radius is the standard for cards and inputs, providing a soft, modern SaaS feel. Larger containers and decorative elements use `rounded-xl` (1.5rem / 24px) to emphasize the shift away from rigid industrial geometry toward a more human-centric interface.

## Components
- **Buttons:** Primary buttons use the Minty Action Green with a subtle top-down gradient and rounded-md corners. Secondary buttons use a ghost style with a Slate-200 border.
- **Cards:** Elevated with Level 1 shadows and 8px rounded corners. No borders are used unless the card is placed on a white background, in which case a 1px Slate-100 border is added.
- **Input Fields:** Use a Slate-50 background and a 1px Slate-200 border that transforms to a 2px Minty Green border on focus.
- **Chips/Badges:** Pill-shaped (rounded-full) with low-opacity background tints of the status color (e.g., 10% Green for "Safe").
- **Lists:** Rows are separated by generous whitespace and a subtle horizontal rule (Slate-50) rather than boxed containers to keep the "airy" feel.
- **Data Tables:** Headers are pinned using the glassmorphic blur effect to keep the interface feeling light while scrolling.