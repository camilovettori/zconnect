import { cookies } from "next/headers";
import { redirect } from "next/navigation";

export default function Home() {
  const session = cookies().get("zconnect_session")?.value;
  redirect(session ? "/sync" : "/login");
}

