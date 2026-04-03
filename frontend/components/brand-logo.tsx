import Image from "next/image";

export function BrandLogo({
  width = 40,
  height = 40,
  className = "",
}: {
  width?: number;
  height?: number;
  className?: string;
}) {
  return (
    <div
      className={[
        "flex items-center justify-center overflow-hidden rounded-2xl bg-white",
        "shadow-[0_18px_40px_-24px_rgba(15,23,42,0.45)]",
        className,
      ].join(" ")}
      style={{ width, height }}
    >
      <Image
        src="/log.png"
        alt="Zconnect logo"
        width={width}
        height={height}
        className="h-auto w-auto object-contain"
        priority
        unoptimized
      />
    </div>
  );
}
