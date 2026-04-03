import Image from "next/image";

export function BrandLogo({
  size = 40,
  className = "",
}: {
  size?: number;
  className?: string;
}) {
  return (
    <div
      className={[
        "relative overflow-hidden rounded-2xl bg-white",
        "shadow-[0_18px_40px_-24px_rgba(15,23,42,0.45)]",
        className,
      ].join(" ")}
      style={{ width: size, height: size }}
    >
      <Image
        src="/log.png"
        alt="Ziffera logo"
        fill
        sizes={`${size}px`}
        className="object-contain p-1.5"
        priority
      />
    </div>
  );
}
