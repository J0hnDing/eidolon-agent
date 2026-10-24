import { Link } from "react-router-dom";
import ChatPage from "./ChatPage";

export default function SkillBuildPage() {
  return <section className="page stack build-page">
    <header className="page-header"><div><Link className="back-link" to="/skills">← Skills</Link><h1>Build a skill</h1></div></header>
    <div className="build-workspace"><ChatPage surface="build" /></div>
  </section>;
}
