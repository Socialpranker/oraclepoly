/**
 * EditorialSkeleton — универсальный skeleton для /bot-терминалов (dry + live).
 * Повторяет общую структуру hero + row + list, используя только CSS.
 */
export function EditorialSkeleton({ label = 'loading' }: { label?: string }) {
  return (
    <div className="ed-skeleton" aria-busy="true" aria-label={label}>
      {/* Hero block */}
      <section
        className="relative pt-5 pb-6 px-5"
        style={{ borderBottom: '1px solid var(--ed-rule)' }}
      >
        <div className="flex items-center justify-between mb-3">
          <SkelBar w={110} h={9} />
          <SkelBar w={60} h={9} />
        </div>
        <SkelBar w={220} h={48} rounded={4} />
        <div className="flex items-center gap-3 mt-4">
          <SkelBar w={80} h={12} />
          <SkelBar w={60} h={10} />
          <div className="flex-1" />
          <SkelBar w={90} h={10} />
        </div>
        <SkelBar w="100%" h={48} className="mt-4" rounded={0} />
      </section>

      {/* Today row */}
      <section
        className="px-5 py-4 flex items-baseline gap-5"
        style={{ borderBottom: '1px solid var(--ed-rule)' }}
      >
        <SkelBar w={72} h={22} />
        <SkelBar w={56} h={14} />
        <SkelBar w={56} h={14} />
        <div className="flex-1" />
        <SkelBar w={40} h={12} />
      </section>

      {/* Strategy rows */}
      {[0, 1, 2].map((i) => (
        <section
          key={i}
          className="px-5 py-4"
          style={{ borderTop: i === 0 ? 'none' : '1px solid var(--ed-rule)' }}
        >
          <div className="flex items-center gap-3 mb-2">
            <SkelBar w={120} h={13} />
            <div className="flex-1" />
            <SkelBar w={32} h={32} rounded={8} />
          </div>
          <div className="flex items-baseline gap-5">
            <SkelBar w={60} h={16} />
            <SkelBar w={44} h={12} />
            <SkelBar w={44} h={12} />
          </div>
        </section>
      ))}
    </div>
  );
}

function SkelBar({
  w,
  h,
  rounded = 2,
  className = '',
}: {
  w: number | string;
  h: number;
  rounded?: number;
  className?: string;
}) {
  return (
    <span
      className={`ed-skel-bar ${className}`}
      style={{
        width: typeof w === 'number' ? `${w}px` : w,
        height: h,
        borderRadius: rounded,
      }}
    />
  );
}
